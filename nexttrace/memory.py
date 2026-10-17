"""Incident memory and agent coordination, stored in the same graph as the service map (one graph per team = tenant).

  (:Alarm {key, service, signal, at})-[:ON]->(:Service)            raw signals; key = service|signal|time window
  (:Incident {key, id, title, status, version, cause, ...})
        -[:HAS_ALARM]->(:Alarm)  -[:AFFECTS]->(:Service)  -[:CAUSED_BY]->(:Service)  -[:FIXED_BY {worked}]->(:Runbook)
  (:Event {seq, at, agent, action, from_status, to_status, note})-[:OF]->(:Incident)     the handoff timeline
  (:Runbook {key, steps, attempts, successes})                                         what fixed it, and how often
  (:Session {id, agent, task})<-[:IN]-(:Step {seq, at, tool, input, output})          persistent agent history

Status flow: open -> investigating -> diagnosed -> resolved. Every change is a compare-and-set on `version`, so of
several agents racing for the same incident exactly one wins (ConflictError for the others). Alarm, incident and
runbook keys are unique-constrained, so parallel writers converge instead of duplicating.
No model runs here: agents (nexttrace.agents, or an LLM over MCP) call these functions.
"""
import json
import time
import uuid

from nexttrace.graphdb import ensure_unique, write

STATUSES = ("open", "investigating", "diagnosed", "resolved")
WINDOW_S = 300  # alarms on the same service and signal within 5 minutes are the same alarm


class ConflictError(RuntimeError):
    """Another agent changed the incident first."""


def _now() -> int:
    return int(time.time())


def _ensure(g):
    for label, prop in (("Alarm", "key"), ("Incident", "key"), ("Runbook", "key"), ("Session", "id")):
        ensure_unique(g, label, prop)


def _service(g, name):
    if not g.ro_query("MATCH (s:Service {name: $n}) RETURN count(s)", {"n": name}).result_set[0][0]:
        raise ValueError(f"no service {name!r} in this map")


# ---------- triage: alarms -> incidents ----------

def _connected(g, a, b, hops):
    if a == b:
        return True
    q = (f"MATCH (x:Service {{name: $a}}), (y:Service {{name: $b}}) MATCH p = (x)-[:CALLS*1..{hops}]-(y) "
         "WHERE all(r IN relationships(p) WHERE NOT coalesce(r.stale, false)) RETURN 1 LIMIT 1")
    return bool(g.ro_query(q, {"a": a, "b": b}).result_set)


def group_alarms(g, services, hops=3):
    """Group alarmed services that are linked through the map within `hops` calls (either direction).
    Alarms on unrelated parts of the system become separate incidents."""
    parent = {s: s for s in services}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    names = sorted(services)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if find(a) != find(b) and _connected(g, a, b, hops):
                parent[find(a)] = find(b)
    groups = {}
    for s in names:
        groups.setdefault(find(s), []).append(s)
    return sorted(groups.values())


OPEN_INCIDENT = """
MERGE (i:Incident {key: $key})
ON CREATE SET i.id = $id, i.title = $title, i.status = 'open', i.version = 0, i.opened_at = $at, i.updated_at = $at,
              i.opened_by = $agent, i.token = $token
RETURN i.id, i.version, i.token = $token
"""


def triage(g, alarms, agent="triage", hops=3):
    """Turn raw alarms ([{service, signal, at?}]) into incidents, idempotently.

    Unknown services are rejected (an alarm must land on the map). Alarms are grouped by how the services are linked
    on the map; each group becomes one incident whose key is derived from its alarms, so parallel triage agents seeing
    the same alarms open the same incident, not two."""
    if not alarms:
        raise ValueError("no alarms given")
    _ensure(g)
    rows = []
    for a in alarms:
        service, signal = str(a.get("service", "")), str(a.get("signal", "")).strip()
        if not signal:
            raise ValueError(f"alarm on {service!r} has no signal")
        _service(g, service)
        at = int(a.get("at") or _now())
        rows.append({"service": service, "signal": signal, "at": at,
                     "key": f"{service}|{signal}|{at // WINDOW_S}"})
    write(g, "UNWIND $rows AS r MATCH (s:Service {name: r.service}) MERGE (a:Alarm {key: r.key}) "
            "ON CREATE SET a.service = r.service, a.signal = r.signal, a.at = r.at MERGE (a)-[:ON]->(s)", {"rows": rows})
    out = []
    for group in group_alarms(g, {r["service"] for r in rows}, hops):
        mine = sorted({r["key"] for r in rows if r["service"] in group})
        key = mine[0]  # deterministic: the same alarms give the same incident
        n = write(g, "MERGE (c:Counter {name: 'incident'}) SET c.n = coalesce(c.n, 0) + 1 RETURN c.n").result_set[0][0]
        title = f"{len(mine)} alarm(s) on " + ", ".join(group)
        at = _now()
        inc_id, version, created = write(g, OPEN_INCIDENT, {"key": key, "id": f"INC-{n}", "title": title, "at": at,
                                                             "agent": agent, "token": uuid.uuid4().hex}).result_set[0]
        write(g, "UNWIND $keys AS k MATCH (i:Incident {key: $key}), (a:Alarm {key: k})-[:ON]->(s:Service) "
                "MERGE (i)-[:HAS_ALARM]->(a) MERGE (i)-[:AFFECTS]->(s)", {"key": key, "keys": mine})
        if created:
            _event(g, inc_id, agent, "opened", "", "open", title)
        out.append({"incident": inc_id, "services": group, "alarms": len(mine), "created": bool(created)})
    return out


# ---------- handoffs ----------

def _event(g, inc_id, agent, action, from_status, to_status, note=""):
    write(g, "MATCH (i:Incident {id: $id}) "
            "CREATE (:Event {seq: i.version, at: $at, agent: $agent, action: $action, from_status: $f, "
            "to_status: $t, note: $note})-[:OF]->(i)",
            {"id": inc_id, "at": _now(), "agent": agent, "action": action, "f": from_status, "t": to_status,
             "note": str(note)[:500]})


def get_incident(g, inc_id):
    rows = g.ro_query("""MATCH (i:Incident {id: $id})
        OPTIONAL MATCH (i)-[:AFFECTS]->(s:Service) WITH i, collect(DISTINCT s.name) AS services
        OPTIONAL MATCH (i)-[:HAS_ALARM]->(a:Alarm) WITH i, services, collect(DISTINCT {service: a.service, signal: a.signal}) AS alarms
        OPTIONAL MATCH (i)-[:CAUSED_BY]->(c:Service)
        RETURN i.id, i.title, i.status, i.version, i.owner, services, alarms, c.name, i.reason, i.evidence, i.fix""",
                      {"id": inc_id}).result_set
    if not rows:
        raise ValueError(f"no incident {inc_id!r}")
    i, title, status, version, owner, services, alarms, cause, reason, evidence, fix = rows[0]
    return {"incident": i, "title": title, "status": status, "version": version, "owner": owner or "",
            "services": sorted(services), "alarms": sorted(alarms, key=lambda a: (a["service"], a["signal"])),
            "cause": cause or "", "reason": reason or "", "evidence": evidence or [], "fix": fix or ""}


CLAIM = """
MATCH (i:Incident {id: $id}) WHERE i.status = $from AND i.version = $v
SET i.status = $to, i.version = $v + 1, i.owner = $agent, i.updated_at = $at
RETURN i.version
"""


def claim(g, inc_id, agent, from_status, to_status, version=None, note=""):
    """Move an incident from one status to the next, only if it is still where the agent saw it. One query, so
    exactly one of several racing agents succeeds; the rest get ConflictError and should re-read or move on."""
    if from_status not in STATUSES or to_status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if STATUSES.index(to_status) != STATUSES.index(from_status) + 1:
        raise ValueError(f"can't move {from_status} -> {to_status}; the flow is {' -> '.join(STATUSES)}")
    cur = get_incident(g, inc_id)
    v = cur["version"] if version is None else version
    rows = write(g, CLAIM, {"id": inc_id, "from": from_status, "to": to_status, "v": v, "agent": agent,
                           "at": _now()}).result_set
    if not rows:
        now = get_incident(g, inc_id)
        raise ConflictError(f"{inc_id} is {now['status']} (version {now['version']}, owner {now['owner'] or '-'}), "
                            f"not {from_status} at version {v}")
    _event(g, inc_id, agent, "claimed", from_status, to_status, note)
    return rows[0][0]


def record_finding(g, inc_id, agent, cause, reason, cited):
    """The investigator's conclusion: the cause service and the connections that support it.

    cited: [(caller, callee), ...] or [{"caller", "callee"}, ...]. Every cited connection must exist on the map and be
    live; the stored evidence is the map's own (file:line), not text from the agent. Requires the agent to own the
    incident in `investigating`, and moves it to `diagnosed`."""
    _service(g, cause)
    cur = get_incident(g, inc_id)
    if cur["status"] != "investigating" or cur["owner"] != agent:
        raise ConflictError(f"{inc_id} is {cur['status']} owned by {cur['owner'] or '-'}; claim it first")
    pairs = [(c["caller"], c["callee"]) if isinstance(c, dict) else tuple(c) for c in cited]
    evidence = []
    for a, b in pairs:
        rows = g.ro_query("MATCH (:Service {name: $a})-[c:CALLS]->(:Service {name: $b}) "
                          "RETURN c.evidence[0], coalesce(c.stale, false)", {"a": a, "b": b}).result_set
        if not rows or rows[0][1]:
            raise ValueError(f"cited connection {a} -> {b} is not on the map" + (" (stale)" if rows else ""))
        evidence.append(f"{a} -> {b} | {rows[0][0]}")
    rows = write(g, "MATCH (i:Incident {id: $id}), (c:Service {name: $cause}) "
                   "WHERE i.status = 'investigating' AND i.owner = $agent AND i.version = $v "
                   "SET i.status = 'diagnosed', i.version = $v + 1, i.reason = $reason, i.evidence = $ev, "
                   "i.updated_at = $at MERGE (i)-[:CAUSED_BY]->(c) RETURN i.version",
                   {"id": inc_id, "cause": cause, "agent": agent, "v": cur["version"], "reason": str(reason)[:1000],
                    "ev": evidence, "at": _now()}).result_set
    if not rows:
        raise ConflictError(f"{inc_id} changed while recording the finding")
    _event(g, inc_id, agent, "diagnosed", "investigating", "diagnosed", f"cause {cause}: {reason}")
    return {"incident": inc_id, "cause": cause, "evidence": evidence, "version": rows[0][0]}


def resolve(g, inc_id, agent, fix, worked=True, runbook=""):
    """Close a diagnosed incident with the fix applied, and update the runbook's attempt and success counts in the
    same query (no lost updates when several incidents close at once)."""
    cur = get_incident(g, inc_id)
    if cur["status"] != "diagnosed":
        raise ConflictError(f"{inc_id} is {cur['status']}; only a diagnosed incident can be resolved")
    if not str(fix).strip():
        raise ValueError("describe the fix")
    _ensure(g)
    key = runbook or f"{cur['cause']}: {str(fix).strip()[:80]}"
    rows = write(g, """MATCH (i:Incident {id: $id}) WHERE i.status = 'diagnosed' AND i.version = $v
        MERGE (r:Runbook {key: $key}) ON CREATE SET r.steps = $fix, r.attempts = 0, r.successes = 0, r.service = $cause
        SET r.attempts = r.attempts + 1, r.successes = r.successes + CASE WHEN $worked THEN 1 ELSE 0 END,
            i.status = CASE WHEN $worked THEN 'resolved' ELSE 'diagnosed' END, i.version = $v + 1, i.fix = $fix,
            i.owner = $agent, i.updated_at = $at, i.resolved_at = CASE WHEN $worked THEN $at ELSE null END
        MERGE (i)-[f:FIXED_BY]->(r) SET f.worked = $worked
        RETURN r.attempts, r.successes, i.status""",
                   {"id": inc_id, "v": cur["version"], "key": key, "fix": str(fix)[:1000], "cause": cur["cause"],
                    "worked": bool(worked), "agent": agent, "at": _now()}).result_set
    if not rows:
        raise ConflictError(f"{inc_id} changed while resolving")
    attempts, successes, status = rows[0]
    _event(g, inc_id, agent, "resolved" if worked else "fix failed", "diagnosed", status, fix)
    return {"incident": inc_id, "status": status, "runbook": key, "attempts": attempts, "successes": successes}


def list_incidents(g, status="", limit=50):
    if status and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    rows = g.ro_query("MATCH (i:Incident) WHERE $st = '' OR i.status = $st OPTIONAL MATCH (i)-[:CAUSED_BY]->(c:Service) "
                      "RETURN i.id, i.title, i.status, i.owner, c.name, i.updated_at ORDER BY i.updated_at DESC LIMIT $n",
                      {"st": status, "n": max(1, min(int(limit), 500))}).result_set
    return [{"incident": i, "title": t, "status": st, "owner": o or "", "cause": c or "", "updated_at": u}
            for i, t, st, o, c, u in rows]


def timeline(g, inc_id):
    get_incident(g, inc_id)
    return [{"seq": s, "at": at, "agent": a, "action": act, "from": f, "to": t, "note": n} for s, at, a, act, f, t, n in
            g.ro_query("MATCH (e:Event)-[:OF]->(:Incident {id: $id}) RETURN e.seq, e.at, e.agent, e.action, "
                       "e.from_status, e.to_status, e.note ORDER BY e.seq, e.at", {"id": inc_id}).result_set]


# ---------- memory recall ----------

def similar_incidents(g, services, cause="", limit=5, exclude=""):
    """Resolved incidents that looked like this one: overlap of affected services (Jaccard), plus a bonus when the cause
    matches. Returns what fixed each, and how often that fix has worked."""
    services = set(services)
    if not services:
        raise ValueError("give the affected services")
    rows = g.ro_query("""MATCH (i:Incident {status: 'resolved'}) WHERE i.id <> $ex
        OPTIONAL MATCH (i)-[:AFFECTS]->(s:Service) WITH i, collect(s.name) AS svcs
        OPTIONAL MATCH (i)-[:CAUSED_BY]->(c:Service)
        OPTIONAL MATCH (i)-[f:FIXED_BY {worked: true}]->(r:Runbook)
        RETURN i.id, i.title, svcs, c.name, i.fix, r.key, r.attempts, r.successes, i.resolved_at""",
                      {"ex": exclude}).result_set
    out = []
    for inc, title, svcs, c, fix, rb, att, succ, at in rows:
        overlap = len(services & set(svcs)) / len(services | set(svcs)) if svcs else 0.0
        score = round(overlap + (0.5 if cause and c == cause else 0.0), 3)
        if score > 0:
            out.append({"incident": inc, "title": title, "score": score, "services": sorted(svcs), "cause": c or "",
                        "fix": fix or "", "runbook": rb or "", "worked": f"{succ}/{att}" if att else "",
                        "resolved_at": at})
    return sorted(out, key=lambda r: (-r["score"], -(r["resolved_at"] or 0)))[:max(1, int(limit))]


# ---------- persistent agent history ----------

def start_session(g, agent, task=""):
    _ensure(g)
    sid = f"S-{uuid.uuid4().hex[:10]}"
    write(g, "CREATE (:Session {id: $id, agent: $agent, task: $task, started_at: $at, steps: 0})",
            {"id": sid, "agent": agent, "task": str(task)[:500], "at": _now()})
    return sid


def log_step(g, session, tool, input=None, output=None, incident=""):
    """Append one step (a tool call and a summary of its result) to a session. Sequence numbers come from the session
    node in the same query, so steps from parallel calls never collide."""
    def short(x):
        return (x if isinstance(x, str) else json.dumps(x, default=str))[:2000] if x is not None else ""
    rows = write(g, "MATCH (s:Session {id: $sid}) SET s.steps = s.steps + 1 "
                   "CREATE (:Step {seq: s.steps, at: $at, tool: $tool, input: $in, output: $out, incident: $inc})-[:IN]->(s) "
                   "RETURN s.steps", {"sid": session, "at": _now(), "tool": str(tool), "in": short(input),
                                      "out": short(output), "inc": incident}).result_set
    if not rows:
        raise ValueError(f"no session {session!r}; start one first")
    if incident:
        write(g, "MATCH (s:Session {id: $sid}), (i:Incident {id: $inc}) MERGE (s)-[:WORKED_ON]->(i)",
                {"sid": session, "inc": incident})
    return rows[0][0]


def history(g, session="", agent="", incident="", limit=50):
    """Past steps, newest first: of one session, of one agent's sessions, or of everything done on one incident."""
    if not (session or agent or incident):
        raise ValueError("give a session, an agent or an incident")
    rows = g.ro_query("""MATCH (st:Step)-[:IN]->(s:Session)
        WHERE ($sid = '' OR s.id = $sid) AND ($agent = '' OR s.agent = $agent) AND ($inc = '' OR st.incident = $inc)
        RETURN s.id, s.agent, s.task, st.seq, st.at, st.tool, st.input, st.output, st.incident
        ORDER BY st.at DESC, st.seq DESC LIMIT $limit""",
                      {"sid": session, "agent": agent, "inc": incident, "limit": max(1, min(int(limit), 500))}).result_set
    return [{"session": s, "agent": a, "task": t, "seq": q, "at": at, "tool": tool, "input": i, "output": o,
             "incident": inc} for s, a, t, q, at, tool, i, o, inc in rows]
