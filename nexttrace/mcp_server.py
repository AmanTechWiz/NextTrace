"""NextTrace MCP server: lets Claude Code (or any MCP client) map repos, read the maps, and fill gaps.

Mapping is the deterministic parser in nexttrace.mapper; no LLM runs here. The connecting agent can add what
the parser missed with add_connection, which only accepts a quote the server finds in the code.

Run:  uv run python -m nexttrace.mcp_server        (stdio)
Env:  FALKORDB_HOST (localhost), FALKORDB_PORT (6379), FALKORDB_USERNAME, FALKORDB_PASSWORD, FALKORDB_TLS=true
      (the same names the official FalkorDB MCP server uses)
"""
import functools
import os
import re
from pathlib import Path
from typing import Any

import redis
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nexttrace import alarms as alarm_source
from nexttrace import failure, mapper
from nexttrace import memory as mem
from nexttrace.graphdb import connect

server = MCPServer(
    name="nexttrace",
    instructions=(
        "NextTrace keeps a graph of a system's services and who calls whom, built from real repos. "
        "Call map_repo once per repo (several repos can share one graph), then list_maps and service_calls to read it. "
        "Every connection carries evidence (file:line or traffic count): cite it instead of guessing. "
        "The parser misses some wiring (registries, message buses, generated clients). Use find_gaps to see where, "
        "read that code, and record each call you confirm with add_connection, quoting the exact line. "
        "Connections you add are stored as inferred, never as fact. For failures use blast_radius (what breaks), "
        "failure_paths (how requests reach a service) and criticality (what matters most); cite their connections. "
        "For questions the tools don't answer, "
        "get_graph_schema then query_graph_readonly runs your own read-only Cypher (including FalkorDB's graph "
        "algorithms); the map itself only changes through the checked tools. "
        "Incidents: triage_alarms opens them; agents hand them on with claim_incident (open -> investigating -> "
        "diagnosed -> resolved), record_finding (cause + cited connections), resolve_incident (fix); similar_incidents "
        "recalls past fixes. Start a session with start_session and pass it to these tools to keep your history."
    ),
)
GRAPH_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SERVICE_CALLS = """
MATCH (s:Service {name: $name})
OPTIONAL MATCH (s)-[o:CALLS]->(callee:Service)
WITH s, collect(CASE WHEN callee IS NULL THEN NULL ELSE {service: callee.name, kind: callee.kind, confidence: o.confidence,
     sources: o.sources, stale: o.stale, evidence: o.evidence} END) AS calls
OPTIONAL MATCH (caller:Service)-[i:CALLS]->(s)
RETURN s.kind, s.owner, s.repo, s.source_dir, s.stale, calls,
       collect(CASE WHEN caller IS NULL THEN NULL ELSE {service: caller.name, confidence: i.confidence,
               sources: i.sources, stale: i.stale, evidence: i.evidence} END) AS called_by
"""


HOST, PORT = os.environ.get("FALKORDB_HOST", "localhost"), int(os.environ.get("FALKORDB_PORT", "6379"))


def _db():
    return connect(HOST, PORT)


def _graph_name(name: str) -> str:
    if not GRAPH_NAME.match(name):
        raise ToolError(f"graph name must be 1-64 letters, digits, '_' or '-': {name!r}")
    return name


def _friendly(fn):
    """Turn a missing database into a message the agent can act on, instead of a generic tool crash."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except redis.exceptions.ConnectionError as e:
            raise ToolError(f"FalkorDB is not reachable at {HOST}:{PORT}. Start it (docker start falkordb) and retry.") from e
        except redis.exceptions.AuthenticationError as e:
            raise ToolError("FalkorDB refused the login: check FALKORDB_USERNAME and FALKORDB_PASSWORD") from e
        except redis.exceptions.ResponseError as e:  # anything FalkorDB rejects: say what, not "tool crashed"
            raise ToolError(f"FalkorDB error: {e}") from e
    return run


@server.tool(annotations=ToolAnnotations(title="Map a repo", idempotent_hint=True), structured_output=True)
@_friendly
def map_repo(path: str, graph: str = "", traces: list[str] | None = None) -> dict[str, Any]:
    """Scan a local repo and write its services and calls into a FalkorDB graph.

    path: absolute path to the repo folder.
    graph: graph name; reuse the same name to put several repos into one map. Defaults to the repo folder name.
    traces: optional CSV files with spanID, parentSpanID, serviceName (recorded traffic) to confirm calls.
    Re-running is safe: unchanged calls stay, vanished ones are marked stale, nothing is deleted.
    """
    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise ToolError(f"not a folder: {repo}")
    for t in traces or []:
        if not Path(t).is_file():
            raise ToolError(f"traces file not found: {t}")
    name = _graph_name(graph or re.sub(r"[^A-Za-z0-9_-]", "_", repo.name))
    try:
        summary = mapper.map_repo(repo, name, traces or [])
    except ValueError as e:  # e.g. a traces CSV without spanID / parentSpanID / serviceName
        raise ToolError(f"could not read traces: {e}") from e
    if summary["services"] == 0:
        summary["hint"] = ("No services found. NextTrace looks for Kubernetes Deployments, docker-compose services, "
                           "Backstage catalog-info.yaml, or Dockerfiles.")
    return summary


@server.tool(annotations=ToolAnnotations(title="List service maps", read_only_hint=True), structured_output=True)
@_friendly
def list_maps() -> list[dict[str, Any]]:
    """List graphs that hold a NextTrace service map, with the repos and commits mapped into each."""
    db, out = _db(), []
    for name in sorted(db.list_graphs()):
        g = db.select_graph(name)
        try:
            runs = g.ro_query("MATCH (r:MapRun) RETURN r.repo, r.commit, r.run ORDER BY r.run").result_set
        except Exception:  # not a NextTrace graph
            continue
        if not runs:
            continue
        counts = g.ro_query("MATCH (s:Service) WITH count(s) AS n OPTIONAL MATCH ()-[c:CALLS]->() "
                            "RETURN n, count(c), toInteger(sum(CASE WHEN c.stale THEN 1 ELSE 0 END))").result_set[0]
        latest = {repo: commit for repo, commit, _ in runs}
        out.append({"graph": name, "services": counts[0], "calls": counts[1], "stale_calls": counts[2],
                    "repos": [{"repo": r, "commit": c} for r, c in latest.items()]})
    return out


def _require_service(g, graph, service):
    if g.ro_query("MATCH (s:Service {name: $name}) RETURN count(s)", {"name": service}).result_set[0][0]:
        return
    names = [r[0] for r in g.ro_query("MATCH (s:Service) RETURN s.name ORDER BY s.name").result_set]
    close = [n for n in names if service.lower() in n.lower()][:10]
    raise ToolError(f"no service {service!r} in graph {graph!r}" + (f"; did you mean {close}?" if close else ""))


@server.tool(annotations=ToolAnnotations(title="Service connections", read_only_hint=True), structured_output=True)
@_friendly
def service_calls(graph: str, service: str) -> dict[str, Any]:
    """What a service calls and what calls it, each with confidence and evidence (file:line or traffic count)."""
    g = _db().select_graph(_graph_name(graph))
    _require_service(g, graph, service)
    rows = g.ro_query(SERVICE_CALLS, {"name": service}).result_set
    kind, owner, repo, source_dir, stale, calls, called_by = rows[0]
    return {"service": service, "kind": kind, "owner": owner, "repo": repo, "source_dir": source_dir, "stale": stale,
            "calls": sorted(calls, key=lambda c: c["service"]), "called_by": sorted(called_by, key=lambda c: c["service"])}



ENDPOINTS = """
MATCH (s:Service {name: $name})-[:EXPOSES]->(e:Endpoint)
WHERE $path = '' OR e.path CONTAINS $path
OPTIONAL MATCH (caller:Service)-[c:CALLS_ENDPOINT]->(e)
RETURN e.method, e.path, e.where, e.declared, e.stale,
       collect(CASE WHEN caller IS NULL THEN NULL ELSE {service: caller.name, evidence: c.evidence, stale: c.stale} END)
ORDER BY e.path, e.method
"""

OUTGOING = """
MATCH (s:Service {name: $name})-[c:CALLS_ENDPOINT]->(e:Endpoint)
RETURN e.service, e.method, e.path, c.matched, c.evidence ORDER BY e.service, e.path
"""


@server.tool(annotations=ToolAnnotations(title="Endpoint callers", read_only_hint=True), structured_output=True)
@_friendly
def endpoint_calls(graph: str, service: str, path: str = "") -> dict[str, Any]:
    """Routes a service exposes and who calls each one (file:line), plus the routes it calls in other services.

    path: optional filter, e.g. "/seats" returns only routes whose path contains it.
    A call with matched=false hits a route the callee doesn't declare: a likely bug or a stale call, unless its path
    is built entirely at runtime (shown as /{}), or the route is generated by a framework rather than written in code.
    """
    g = _db().select_graph(_graph_name(graph))
    _require_service(g, graph, service)
    exposes = [{"method": m, "path": p, "where": w, "declared": d, "stale": st,
                "called_by": sorted(c, key=lambda x: x["service"])}
               for m, p, w, d, st, c in g.ro_query(ENDPOINTS, {"name": service, "path": path}).result_set]
    calls = [{"service": sv, "method": m, "path": p, "matched": ok, "evidence": ev}
             for sv, m, p, ok, ev in g.ro_query(OUTGOING, {"name": service}).result_set]
    return {"service": service, "exposes": exposes, "calls": calls}


GAPS = """
MATCH (s:Service {kind: 'service'}) WHERE NOT coalesce(s.stale, false)
OPTIONAL MATCH (s)-[o:CALLS]->(x:Service) WHERE NOT coalesce(o.stale, false)
WITH s, collect(DISTINCT x.kind) AS out_kinds, count(x) AS outs
OPTIONAL MATCH (y:Service)-[i:CALLS]->(s) WHERE NOT coalesce(i.stale, false)
RETURN s.name, s.repo, s.source_dir, outs, count(y), [k IN out_kinds WHERE k = 'service'] <> []
ORDER BY s.name
"""


@server.tool(annotations=ToolAnnotations(title="Where the map may be incomplete", read_only_hint=True), structured_output=True)
@_friendly
def find_gaps(graph: str) -> dict[str, Any]:
    """Where the map may be incomplete. Each list is computed by the server, not guessed; work through all of them.

    unmapped_folders: deployable folders (with a Dockerfile, package.json, pom.xml, go.mod, .csproj, ...) that no
      service covers. `may_belong_to` names mapped services that have no code yet and whose name fits the folder:
      attach those with set_service_folder. Add the other folders that run as a service with add_service; skip
      shared libraries and tools.
    possible_duplicates: pairs that may be one service under two names. Find which name the system really uses
      and merge with merge_services.
    isolated: services with no known calls in or out. no_service_calls: services that call only datastores or infra
      (a frontend or gateway here usually means missed calls). Read their code and add calls with add_connection.
    """
    g = _db().select_graph(_graph_name(graph))
    rows = g.ro_query(GAPS).result_set
    roots = dict(g.ro_query("MATCH (r:MapRun) RETURN r.repo, r.root ORDER BY r.run").result_set)
    every = g.ro_query("MATCH (s:Service) WHERE NOT coalesce(s.stale, false) RETURN s.name, s.repo, s.source_dir").result_set
    if not every:
        raise ToolError(f"graph {graph!r} has no services; run map_repo first")
    isolated, no_service_calls = [], []
    for name, repo, src, outs, ins, calls_service in rows:
        item = {"service": name, "folder": str(Path(roots.get(repo, "")) / src) if src else "", "callers": ins}
        if outs == 0 and ins == 0:
            isolated.append(item)
        elif not calls_service:
            no_service_calls.append(item)
    unmapped = []
    for repo, root in roots.items():
        if not root or not Path(root).is_dir():
            continue
        folderless = [n for n, r, d in every if r == repo and not d]
        for f in mapper.unmapped_folders(Path(root), [d for _, r, d in every if r == repo]):
            item = {"folder": str(Path(root) / f)}
            match = [n for n in folderless if mapper.folder_matches(f, n)]
            if match:  # a service the scanner found without code, whose name fits this folder: attach with set_service_folder
                item["may_belong_to"] = match
            unmapped.append(item)
    dups = mapper.possible_duplicates([(n, str(Path(roots.get(r) or "") / d) if d else "") for n, r, d in every])
    return {"graph": graph, "services": len(rows), "unmapped_folders": unmapped, "possible_duplicates": dups,
            "isolated": isolated, "no_service_calls": no_service_calls}


ADD = """
MATCH (a:Service {name: $caller}), (b:Service {name: $callee})
MERGE (a)-[c:CALLS]->(b)
ON CREATE SET c.sources = ['ai'], c.confidence = 'inferred', c.evidence = [$evidence], c.evidence_count = 1,
              c.repo = a.repo, c.last_run = $run, c.stale = false, c.reason = $reason
RETURN c.sources, c.confidence, c.evidence
"""


@server.tool(annotations=ToolAnnotations(title="Add a call you found in the code", idempotent_hint=True), structured_output=True)
@_friendly
def add_connection(graph: str, caller: str, callee: str, file: str, line: int, quote: str, reason: str = "") -> dict[str, Any]:
    """Record that `caller` calls `callee`, with proof. The server checks the proof before storing anything.

    file: path inside the caller's repo (relative to the repo root, or absolute).
    line: line number of the call. quote: the exact code on that line (copy it; must mention the callee).
    reason: one sentence on how the call works, e.g. "Feign client resolved through Eureka".
    Stored as confidence=inferred, sources=['ai']. If the parser already has this call, nothing changes.
    """
    g = _db().select_graph(_graph_name(graph))
    if caller == callee:
        raise ToolError("caller and callee are the same service")
    for name in (caller, callee):
        _require_service(g, graph, name)
    repo = g.ro_query("MATCH (s:Service {name: $n}) RETURN s.repo", {"n": caller}).result_set[0][0]
    run = g.ro_query("MATCH (r:MapRun {repo: $repo}) RETURN r.root, r.run ORDER BY r.run DESC LIMIT 1",
                     {"repo": repo}).result_set
    if not run or not run[0][0]:
        raise ToolError(f"repo {repo!r} has no recorded folder; run map_repo on it again")
    try:
        rel, at = mapper.verify_quote(Path(run[0][0]), file, line, quote, callee)
    except ValueError as e:
        raise ToolError(f"not added: {e}") from e
    evidence = f"ai | {rel}:{at} | {' '.join(quote.split())[:160]}"
    sources, confidence, stored = g.query(ADD, {"caller": caller, "callee": callee, "evidence": evidence,
                                                 "run": run[0][1], "reason": reason[:300]}).result_set[0]
    return {"caller": caller, "callee": callee, "sources": sources, "confidence": confidence, "evidence": stored,
            "already_known": sources != ["ai"]}


@server.tool(annotations=ToolAnnotations(title="Save a map to a file", idempotent_hint=True), structured_output=True)
@_friendly
def export_map(graph: str, path: str) -> dict[str, Any]:
    """Write the graph's services and current (non-stale) calls to a JSON file, exactly as stored.

    path: absolute path ending in .json; its folder must exist. The server writes the file, so it is a faithful
    copy of the graph (parser and agent-added calls, each with sources, confidence and evidence).
    """
    import json
    out = Path(path).expanduser()
    if not out.is_absolute() or out.suffix != ".json":
        raise ToolError("path must be an absolute path ending in .json")
    if not out.parent.is_dir():
        raise ToolError(f"folder does not exist: {out.parent}")
    g = _db().select_graph(_graph_name(graph))
    services = [{"name": n, "kind": k, "repo": r} for n, k, r in g.ro_query(
        "MATCH (s:Service) WHERE NOT coalesce(s.stale, false) RETURN s.name, s.kind, s.repo ORDER BY s.name").result_set]
    if not services:
        raise ToolError(f"graph {graph!r} has no services; run map_repo first")
    calls = [{"caller": a, "callee": b, "sources": src, "confidence": conf, "evidence": ev} for a, b, src, conf, ev in g.ro_query(
        "MATCH (a:Service)-[c:CALLS]->(b:Service) WHERE NOT coalesce(c.stale, false) "
        "RETURN a.name, b.name, c.sources, c.confidence, c.evidence ORDER BY a.name, b.name").result_set]
    runs = g.ro_query("MATCH (r:MapRun) RETURN r.repo, r.commit ORDER BY r.run").result_set
    out.write_text(json.dumps({"graph": graph, "repos": dict(runs), "services": services, "calls": calls}, indent=2) + "\n")
    return {"path": str(out), "services": len(services), "calls": len(calls),
            "agent_added": sum(1 for c in calls if c["sources"] == ["ai"])}


def _repo_root(g, repo: str = ""):
    """(repo, root, latest run) for the repo a tool call is about; `repo` is required when the graph has several."""
    runs = g.ro_query("MATCH (r:MapRun) RETURN r.repo, r.root, r.run ORDER BY r.run").result_set
    latest = {r: (root, run) for r, root, run in runs}
    if not latest:
        raise ToolError("this graph has no mapped repo; run map_repo first")
    if not repo:
        if len(latest) > 1:
            raise ToolError(f"this graph holds several repos {sorted(latest)}; pass repo=")
        repo = next(iter(latest))
    if repo not in latest or not latest[repo][0]:
        raise ToolError(f"repo {repo!r} is not mapped in this graph (mapped: {sorted(latest)})")
    return repo, Path(latest[repo][0]), latest[repo][1]


ADD_SERVICE = """
MERGE (s:Service {name: $name})
ON CREATE SET s.kind = $kind, s.source_dir = $source_dir, s.declared_in = $declared_in, s.owner = '', s.repo = $repo,
              s.last_run = $run, s.stale = false, s.origin = 'ai', s.evidence = [$evidence], s.reason = $reason
RETURN s.origin
"""


@server.tool(annotations=ToolAnnotations(title="Add a service the parser missed", idempotent_hint=True), structured_output=True)
@_friendly
def add_service(graph: str, name: str, kind: str, file: str, line: int, quote: str, folder: str = "",
                reason: str = "", repo: str = "") -> dict[str, Any]:
    """Add a service, datastore or broker the parser did not find. The server checks the proof first.

    name: the name the system uses for it (how others address it), lowercase. kind: service | datastore | infra.
    file/line/quote: the exact line that declares or names it (an app id, a compose/k8s/Dapr name, a config key);
      the quote must contain the name as a whole word.
    folder: required for kind=service: its code folder, which must contain a build file (Dockerfile, package.json,
      pom.xml, go.mod, .csproj, ...) and must not already belong to another service (then use merge_services).
    Stored as origin=ai with the quote; re-checked on every re-map and marked stale if the line disappears.
    """
    g = _db().select_graph(_graph_name(graph))
    if not mapper.SERVICE_NAME.match(name):
        raise ToolError("name must be lowercase letters, digits, '.', '_' or '-' (max 63)")
    if kind not in ("service", "datastore", "infra"):
        raise ToolError("kind must be service, datastore or infra")
    if g.ro_query("MATCH (s:Service {name: $n}) RETURN count(s)", {"n": name}).result_set[0][0]:
        raise ToolError(f"{name!r} is already in the map; to fix a second name for it, use merge_services")
    repo, root, run = _repo_root(g, repo)
    if not mapper.names_in(quote, name):
        raise ToolError(f"not added: the quote must contain the name {name!r} as a whole word")
    try:
        rel, at = mapper.verify_quote(root, file, line, quote, name)
    except ValueError as e:
        raise ToolError(f"not added: {e}") from e
    source_dir = ""
    if kind == "service":
        if not folder:
            raise ToolError("not added: a service needs its code folder (folder=)")
        f = (root / folder).resolve() if not Path(folder).is_absolute() else Path(folder).resolve()
        if root.resolve() != f and root.resolve() not in f.parents:
            raise ToolError(f"not added: {folder} is outside the repo")
        if not mapper.deployable(f):
            raise ToolError(f"not added: {folder} has no build file, so it is not a deployable service")
        source_dir = str(f.relative_to(root.resolve())) if f != root.resolve() else "."
        owner = g.ro_query("MATCH (s:Service {repo: $repo, source_dir: $d}) RETURN s.name", {"repo": repo, "d": source_dir}).result_set
        if owner:
            raise ToolError(f"not added: {source_dir} is already the code of {owner[0][0]!r}; if that is the same service "
                            "under another name, use merge_services")
    from nexttrace.graphdb import ensure_unique
    ensure_unique(g, "Service", "name")
    evidence = f"ai | {rel}:{at} | {' '.join(quote.split())[:160]}"
    g.query(ADD_SERVICE, {"name": name, "kind": kind, "source_dir": source_dir, "declared_in": rel, "repo": repo,
                          "run": run, "evidence": evidence, "reason": reason[:300]})
    return {"name": name, "kind": kind, "repo": repo, "source_dir": source_dir, "evidence": evidence, "origin": "ai"}


@server.tool(annotations=ToolAnnotations(title="Merge two names of one service", destructive_hint=True), structured_output=True)
@_friendly
def merge_services(graph: str, keep: str, drop: str, file: str, line: int, quote: str, reason: str = "") -> dict[str, Any]:
    """One service appears under two names: keep the name the system really uses, fold the other into it.

    keep: the real name (how callers address it, or how the service names itself). It may not be in the map yet:
      then `drop` is renamed to it. drop: the wrong or duplicate name; its calls, callers and routes move to keep.
    file/line/quote: proof that `keep` is the name in use; the quote must contain `keep` as a whole word.
    The server also needs proof both names are the same code, checked by itself: their folders are the same or
    nested, or both deploy the same image built for this system (public images like mongo don't count), or the
    quote is in drop's own folder (a self-declared name such as spring.application.name), or the quote names both.
    The old name is remembered as an alias, so re-mapping won't split them again.
    """
    g = _db().select_graph(_graph_name(graph))
    if keep == drop:
        raise ToolError("keep and drop are the same name")
    if not mapper.SERVICE_NAME.match(keep):
        raise ToolError("keep must be lowercase letters, digits, '.', '_' or '-' (max 63)")
    _require_service(g, graph, drop)
    d_repo, d_dir, d_img = g.ro_query("MATCH (s:Service {name: $n}) RETURN s.repo, s.source_dir, coalesce(s.images, [])",
                                      {"n": drop}).result_set[0]
    k_row = g.ro_query("MATCH (s:Service {name: $n}) RETURN s.repo, s.source_dir, coalesce(s.images, [])", {"n": keep}).result_set
    repo, root, _ = _repo_root(g, d_repo)
    if not mapper.names_in(quote, keep):
        raise ToolError(f"not merged: the quote must contain {keep!r} as a whole word")
    try:
        rel, at = mapper.verify_quote(root, file, line, quote, keep)
    except ValueError as e:
        raise ToolError(f"not merged: {e}") from e
    same_code = mapper.names_in(quote, drop) or (d_dir and (rel == d_dir or rel.startswith(d_dir.rstrip("/") + "/") or d_dir == "."))
    if k_row and k_row[0][1] and d_dir:
        a, b = Path(k_row[0][1]), Path(d_dir)
        same_code = same_code or a == b or a in b.parents or b in a.parents
    shared = sorted(set(k_row[0][2]) & set(d_img)) if k_row else []
    same_code = same_code or bool(shared)  # both deploy the same image built for this system
    if not same_code:
        raise ToolError("not merged: no proof these are the same service. It needs one of: the same or nested code "
                        "folders, the same image built for this system in both deploy files, a quote from the drop "
                        "service's own folder, or a quote naming both")
    evidence = f"ai | {rel}:{at} | {' '.join(quote.split())[:160]}"
    if not k_row:  # the real name isn't in the map yet: rename
        g.query("MATCH (s:Service {name: $drop}) SET s.name = $keep, s.aliases = coalesce(s.aliases, []) + [$drop], "
                "s.merge_evidence = coalesce(s.merge_evidence, []) + [$ev]", {"drop": drop, "keep": keep, "ev": evidence})
        g.query("MATCH (e:Endpoint {service: $drop}) SET e.service = $keep, e.key = $keep + substring(e.key, size($drop))",
                {"drop": drop, "keep": keep})
        return {"kept": keep, "dropped": drop, "action": "renamed", "evidence": evidence}
    moved = _move_edges(g, drop, keep)
    g.query("MATCH (k:Service {name: $keep}), (d:Service {name: $drop}) "
            "SET k.aliases = coalesce(k.aliases, []) + [$drop] + coalesce(d.aliases, []), "
            "k.merge_evidence = coalesce(k.merge_evidence, []) + [$ev], "
            "k.source_dir = CASE WHEN coalesce(k.source_dir, '') = '' THEN d.source_dir ELSE k.source_dir END "
            "DETACH DELETE d", {"keep": keep, "drop": drop, "ev": evidence})
    return {"kept": keep, "dropped": drop, "action": "merged", "moved": moved, "evidence": evidence}


def _move_edges(g, drop, keep) -> dict:
    """Re-point drop's calls, callers and routes to keep, merging evidence where keep already has the same link."""
    out = g.ro_query("MATCH (:Service {name: $d})-[c:CALLS]->(x:Service) WHERE x.name <> $k RETURN x.name, properties(c)",
                     {"d": drop, "k": keep}).result_set
    inc = g.ro_query("MATCH (x:Service)-[c:CALLS]->(:Service {name: $d}) WHERE x.name <> $k RETURN x.name, properties(c)",
                     {"d": drop, "k": keep}).result_set
    q = ("MATCH (a:Service {name: $a}), (b:Service {name: $b}) MERGE (a)-[c:CALLS]->(b) "
         "ON CREATE SET c += $p "
         "ON MATCH SET c.evidence = c.evidence + [e IN $p.evidence WHERE NOT e IN c.evidence], "
         "c.evidence_count = coalesce(c.evidence_count, 0) + coalesce($p.evidence_count, 0)")
    for x, props in out:
        g.query(q, {"a": keep, "b": x, "p": props})
    for x, props in inc:
        g.query(q, {"a": x, "b": keep, "p": props})
    routes = g.ro_query("MATCH (e:Endpoint {service: $d}) RETURN e.key", {"d": drop}).result_set
    for (key,) in routes:
        new = keep + key[len(drop):]
        if g.ro_query("MATCH (e:Endpoint {key: $k}) RETURN count(e)", {"k": new}).result_set[0][0]:
            g.query("MATCH (a:Service)-[c:CALLS_ENDPOINT]->(:Endpoint {key: $old}), (n:Endpoint {key: $new}) "
                    "MERGE (a)-[m:CALLS_ENDPOINT]->(n) ON CREATE SET m += properties(c)", {"old": key, "new": new})
            g.query("MATCH (e:Endpoint {key: $old}) DETACH DELETE e", {"old": key})
        else:
            g.query("MATCH (e:Endpoint {key: $old}), (k:Service {name: $keep}) SET e.key = $new, e.service = $keep "
                    "MERGE (k)-[:EXPOSES]->(e)", {"old": key, "new": new, "keep": keep})
    return {"calls_out": len(out), "callers": len(inc), "routes": len(routes)}


@server.tool(annotations=ToolAnnotations(title="Attach a code folder to a service", idempotent_hint=True), structured_output=True)
@_friendly
def set_service_folder(graph: str, service: str, folder: str, file: str = "", line: int = 0, quote: str = "",
                       reason: str = "") -> dict[str, Any]:
    """Give a mapped service its code folder (it was found in deploy files only), so its code gets scanned.

    The folder must be inside the repo, deployable (has a build file) and not another service's. Proof, any one:
      - the folder's name fits the service's name by convention and no other free folder fits (no quote needed);
      - a quote from a file inside the folder that names the service (an app id, package or application name);
      - a quote naming both the service and the folder (e.g. a build or CI line).
    folder="" removes a folder an agent attached earlier. Re-run map_repo afterwards so its calls are scanned.
    """
    g = _db().select_graph(_graph_name(graph))
    _require_service(g, graph, service)
    repo, cur, origin = g.ro_query("MATCH (s:Service {name: $n}) RETURN s.repo, coalesce(s.source_dir, ''), s.folder_origin",
                                   {"n": service}).result_set[0]
    if not folder:
        if origin != "ai":
            raise ToolError("only a folder an agent attached can be removed")
        g.query("MATCH (s:Service {name: $n}) SET s.source_dir = '', s.agent_folder = null, s.folder_origin = null, "
                "s.folder_evidence = null", {"n": service})
        return {"service": service, "folder": "", "action": "removed"}
    if cur:
        raise ToolError(f"{service!r} already has its code folder {cur!r}")
    repo, root, _ = _repo_root(g, repo)
    root = root.resolve()
    f = (root / folder).resolve() if not Path(folder).is_absolute() else Path(folder).resolve()
    if root != f and root not in f.parents:
        raise ToolError(f"not set: {folder} is outside the repo")
    if not mapper.deployable(f):
        raise ToolError(f"not set: {folder} has no build file, so it is not a deployable service")
    rel = str(f.relative_to(root)) if f != root else "."
    here = Path(rel)
    taken = [n for n, d in g.ro_query("MATCH (s:Service {repo: $repo}) WHERE s.source_dir <> '' RETURN s.name, s.source_dir",
                                      {"repo": repo}).result_set
             if Path(d) == here or Path(d) in here.parents or here in Path(d).parents or d == "."]
    if taken:
        raise ToolError(f"not set: {rel} overlaps the code of {taken[0]!r}")
    proof = ""
    if quote:
        if not mapper.names_in(quote, service):
            raise ToolError(f"not set: the quote must contain {service!r} as a whole word")
        try:
            qrel, at = mapper.verify_quote(root, file, line, quote, service)
        except ValueError as e:
            raise ToolError(f"not set: {e}") from e
        inside = qrel == rel or qrel.startswith(rel.rstrip("/") + "/") or rel == "."
        if not inside and not any(n in quote for n in [rel] + mapper.folder_names(rel)):
            raise ToolError("not set: the quote must come from a file inside the folder, or name the folder too")
        proof = f"ai | {qrel}:{at} | {' '.join(quote.split())[:160]}"
    else:
        if not mapper.folder_matches(rel, service):
            raise ToolError(f"not set: {rel!r} doesn't match {service!r} by name; cite a line instead (file, line, quote)")
        owned = [d for (d,) in g.ro_query("MATCH (s:Service {repo: $repo}) RETURN s.source_dir", {"repo": repo}).result_set if d]
        rivals = [x for x in mapper.unmapped_folders(root, owned) if x != rel and mapper.folder_matches(x, service)]
        if rivals:
            raise ToolError(f"not set: other free folders also fit {service!r} by name ({rivals[:3]}); cite a line instead")
        proof = f"convention | {rel} | folder name fits {service}"
    g.query("MATCH (s:Service {name: $n}) SET s.source_dir = $d, s.agent_folder = $d, s.folder_origin = 'ai', "
            "s.folder_evidence = $p, s.folder_reason = $r", {"n": service, "d": rel, "p": proof, "r": reason[:300]})
    return {"service": service, "folder": rel, "proof": proof, "action": "set",
            "next": "run map_repo on this repo again so the service's code is scanned"}


@server.tool(annotations=ToolAnnotations(title="Remove a service an agent added", destructive_hint=True), structured_output=True)
@_friendly
def remove_service(graph: str, name: str, reason: str) -> dict[str, Any]:
    """Undo add_service: delete a service an agent added, with its calls. Services the parser found can't be removed
    (they come back on the next map); if one is a duplicate, use merge_services."""
    g = _db().select_graph(_graph_name(graph))
    _require_service(g, graph, name)
    origin = g.ro_query("MATCH (s:Service {name: $n}) RETURN s.origin", {"n": name}).result_set[0][0]
    if origin != "ai":
        raise ToolError(f"{name!r} was found by the parser, not added by an agent; it can't be removed")
    n = g.query("MATCH (s:Service {name: $n}) OPTIONAL MATCH (s)-[c]-() WITH s, count(c) AS c DETACH DELETE s RETURN c",
                {"n": name}).result_set[0][0]
    return {"removed": name, "calls_removed": n, "reason": reason[:300]}


@server.tool(annotations=ToolAnnotations(title="Remove a call an agent added", destructive_hint=True), structured_output=True)
@_friendly
def remove_connection(graph: str, caller: str, callee: str, reason: str) -> dict[str, Any]:
    """Undo add_connection. Only calls added by an agent (sources=['ai']) can be removed."""
    g = _db().select_graph(_graph_name(graph))
    rows = g.ro_query("MATCH (:Service {name: $a})-[c:CALLS]->(:Service {name: $b}) RETURN c.sources",
                      {"a": caller, "b": callee}).result_set
    if not rows:
        raise ToolError(f"no call {caller} -> {callee} in graph {graph!r}")
    if rows[0][0] != ["ai"]:
        raise ToolError(f"{caller} -> {callee} was found by the parser (sources {rows[0][0]}); it can't be removed")
    g.query("MATCH (:Service {name: $a})-[c:CALLS]->(:Service {name: $b}) DELETE c", {"a": caller, "b": callee})
    return {"removed": f"{caller} -> {callee}", "reason": reason[:300]}


# ---------- generic read access (the official FalkorDB MCP server's query and schema tools, read-only) ----------

def _plain(v):
    """FalkorDB values (nodes, edges, paths) as JSON-friendly data."""
    from falkordb import Edge, Node, Path as GPath
    if isinstance(v, Node):
        return {"node": v.id, "labels": list(v.labels), "properties": {k: _plain(x) for k, x in v.properties.items()}}
    if isinstance(v, Edge):
        return {"edge": v.id, "type": v.relation, "from": v.src_node, "to": v.dest_node,
                "properties": {k: _plain(x) for k, x in v.properties.items()}}
    if isinstance(v, GPath):
        return {"path": [_plain(n) for n in v.nodes()], "edges": [_plain(e) for e in v.edges()]}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


@server.tool(annotations=ToolAnnotations(title="List graphs", read_only_hint=True), structured_output=True)
@_friendly
def list_graphs() -> dict[str, Any]:
    """Every graph in the FalkorDB instance, and whether it holds a NextTrace service map."""
    db, out = _db(), []
    for name in sorted(db.list_graphs()):
        try:
            mapped = db.select_graph(name).ro_query("MATCH (r:MapRun) RETURN count(r)").result_set[0][0] > 0
        except Exception:
            mapped = False
        out.append({"graph": name, "nexttrace_map": mapped})
    return {"graphs": out}


@server.tool(annotations=ToolAnnotations(title="Graph schema", read_only_hint=True), structured_output=True)
@_friendly
def get_graph_schema(graph: str, sample: int = 200) -> dict[str, Any]:
    """Labels, relationship types, how they connect, and property names, with counts. Read this before writing Cypher.

    sample: nodes per label used to list property names (counts of properties are within that sample).
    """
    g = _db().select_graph(_graph_name(graph))
    ident = re.compile(r"^[A-Za-z0-9_]+$")
    try:
        labels = [r[0] for r in g.ro_query("CALL db.labels()").result_set]
        rels = [r[0] for r in g.ro_query("CALL db.relationshipTypes()").result_set]
    except redis.exceptions.ResponseError:  # FalkorDB errors on a graph that doesn't exist
        labels = rels = []
    if not labels and not rels:
        raise ToolError(f"graph {graph!r} is empty or does not exist")
    sample = max(1, min(int(sample), 2000))
    out_labels = []
    for label in labels:
        if not ident.match(label):
            continue
        count = g.ro_query(f"MATCH (n:`{label}`) RETURN count(n)").result_set[0][0]
        props = dict(g.ro_query(f"MATCH (n:`{label}`) WITH n LIMIT {sample} UNWIND keys(n) AS k RETURN k, count(*)").result_set)
        out_labels.append({"label": label, "count": count, "properties": props})
    out_rels = []
    for rel in rels:
        if not ident.match(rel):
            continue
        count = g.ro_query(f"MATCH ()-[r:`{rel}`]->() RETURN count(r)").result_set[0][0]
        props = dict(g.ro_query(f"MATCH ()-[r:`{rel}`]->() WITH r LIMIT {sample} UNWIND keys(r) AS k RETURN k, count(*)").result_set)
        out_rels.append({"type": rel, "count": count, "properties": props})
    links = [{"from": a, "type": t, "to": b, "count": n} for a, t, b, n in g.ro_query(
        "MATCH (a)-[r]->(b) RETURN labels(a)[0], type(r), labels(b)[0], count(*) ORDER BY count(*) DESC").result_set]
    return {"graph": graph, "labels": out_labels, "relationship_types": out_rels, "connections": links}


@server.tool(annotations=ToolAnnotations(title="Run a read-only Cypher query", read_only_hint=True), structured_output=True)
@_friendly
def query_graph_readonly(graph: str, query: str, params: dict[str, Any] | None = None, max_rows: int = 200,
                         timeout_ms: int = 5000) -> dict[str, Any]:
    """Run your own Cypher on a graph. Read-only: FalkorDB itself refuses anything that writes.

    Use $name placeholders with params (parameters can't be used in LIMIT/SKIP). FalkorDB supports a subset of
    Cypher plus graph algorithms, e.g. CALL algo.pageRank('Service','CALLS') YIELD node, score,
    CALL algo.betweenness({nodeLabels:['Service'], relationshipTypes:['CALLS']}) YIELD node, score,
    CALL algo.bfs(startNode, maxDepth, 'CALLS') YIELD nodes, edges, and shortestPath((a)-[:CALLS*]->(b)).
    To change the map use the checked tools (add_connection, add_service, ...), not this one.
    max_rows: rows returned (1-1000); timeout_ms: 100-30000.
    """
    g = _db().select_graph(_graph_name(graph))
    max_rows, timeout_ms = max(1, min(int(max_rows), 1000)), max(100, min(int(timeout_ms), 30000))
    try:
        res = g.ro_query(query, params or None, timeout=timeout_ms)
    except redis.exceptions.ResponseError as e:  # bad Cypher, a write, a timeout: tell the agent exactly what
        raise ToolError(f"FalkorDB rejected the query: {e}") from e
    rows = [[_plain(v) for v in row] for row in res.result_set]
    return {"columns": [h[1] if isinstance(h, (list, tuple)) else h for h in res.header], "rows": rows[:max_rows],
            "row_count": len(rows), "truncated": len(rows) > max_rows, "run_time_ms": res.run_time_ms}


# ---------- failure questions (nexttrace.failure): answered in FalkorDB, every answer cites its connections ----------

def _ask(fn, graph, *args, **kwargs):
    g = _db().select_graph(_graph_name(graph))
    try:
        return fn(g, *args, **kwargs)
    except ValueError as e:
        raise ToolError(str(e)) from e


@server.tool(annotations=ToolAnnotations(title="What breaks if a service fails", read_only_hint=True), structured_output=True)
@_friendly
def blast_radius(graph: str, service: str, max_depth: int = 6, include_stale: bool = False) -> dict[str, Any]:
    """Everything that breaks if `service` fails: its callers and their callers (up to max_depth hops), each with the
    shortest chain to the failure, the weakest confidence on that chain, its owner, and the evidence for every hop.
    Also returns the owners to notify, the entry points hit (where users feel it) and every cited connection."""
    return _ask(failure.blast_radius, graph, service, max_depth=max_depth, include_stale=include_stale)


@server.tool(annotations=ToolAnnotations(title="How requests reach a service", read_only_hint=True), structured_output=True)
@_friendly
def failure_paths(graph: str, service: str, from_service: str = "", max_paths: int = 10, max_depth: int = 6,
                  include_stale: bool = False) -> dict[str, Any]:
    """The shortest call chains that reach `service`, from `from_service` or from every entry point (services nothing
    calls). Ties are all kept. Use it to explain how a failure in `service` reaches users, or how A depends on B.
    Each hop cites its evidence; entry points with no route are listed as not_reached."""
    return _ask(failure.failure_paths, graph, service, from_service=from_service, max_paths=max_paths,
                max_depth=max_depth, include_stale=include_stale)


@server.tool(annotations=ToolAnnotations(title="Which services matter most", read_only_hint=True), structured_output=True)
@_friendly
def criticality(graph: str, top: int = 10, service: str = "", kind: str = "", include_stale: bool = False) -> dict[str, Any]:
    """Rank services by how much depends on them: dependents (how many break if it fails), betweenness (bridges and
    single points of failure, FalkorDB algo.betweenness over all sources) and PageRank (FalkorDB algo.pageRank).
    service: get one service's metrics and rank. kind: rank only service | datastore | infra | other.
    Each ranked service cites its direct callers' connections."""
    return _ask(failure.criticality, graph, top=top, service=service, kind=kind, include_stale=include_stale)


# ---------- incidents, handoffs and memory (nexttrace.memory) ----------

def _mem(fn, graph, *args, session="", log_as="", **kwargs):
    """Run a memory operation; when the agent passes its session, record the call and its result as a step."""
    g = _db().select_graph(_graph_name(graph))
    try:
        out = fn(g, *args, **kwargs)
    except mem.ConflictError as e:
        raise ToolError(f"conflict: {e}") from e
    except ValueError as e:
        raise ToolError(str(e)) from e
    if session:
        inc = kwargs.get("inc_id") or (args[0] if args and str(args[0]).startswith("INC-") else "")
        try:
            mem.log_step(g, session, log_as or fn.__name__, {"args": args, **kwargs}, out, incident=inc)
        except ValueError as e:
            raise ToolError(f"done, but not logged: {e}") from e
    return out


@server.tool(annotations=ToolAnnotations(title="Alarms from a metrics file", read_only_hint=True), structured_output=True)
@_friendly
def alarms_from_metrics(graph: str, csv_path: str, since: int, threshold: float = 3.0, top: int = 20) -> dict[str, Any]:
    """Read a metrics CSV (a `time` column in epoch seconds, then `<service>_<metric>` columns) and list metrics that
    moved >= threshold standard deviations after `since`, strongest first. Only services on the map count.
    The loudest alarm is often a victim, not the cause: use blast_radius / failure_paths before blaming it."""
    if not Path(csv_path).is_file():
        raise ToolError(f"no such file: {csv_path}")
    g = _db().select_graph(_graph_name(graph))
    known = [r[0] for r in g.ro_query("MATCH (s:Service) RETURN s.name").result_set]
    try:
        return {"alarms": alarm_source.from_metrics(csv_path, int(since), known, threshold, top)}
    except ValueError as e:
        raise ToolError(str(e)) from e


@server.tool(annotations=ToolAnnotations(title="Open incidents from alarms", idempotent_hint=True), structured_output=True)
@_friendly
def triage_alarms(graph: str, alarms: list[dict[str, Any]], agent: str = "triage", hops: int = 3,
                  session: str = "") -> dict[str, Any]:
    """Turn alarms ([{service, signal, at}]) into incidents: alarms on services linked within `hops` calls on the map
    become one incident; unrelated ones become separate incidents. Idempotent: the same alarms give the same incident,
    even from parallel agents. Every alarm must be on a service in the map."""
    return {"incidents": _mem(mem.triage, graph, alarms, agent=agent, hops=hops, session=session, log_as="triage_alarms")}


@server.tool(annotations=ToolAnnotations(title="List incidents", read_only_hint=True), structured_output=True)
@_friendly
def list_incidents(graph: str, status: str = "", limit: int = 50) -> dict[str, Any]:
    """Incidents, newest first; filter by status (open, investigating, diagnosed, resolved) to find work to pick up."""
    return {"incidents": _mem(mem.list_incidents, graph, status=status, limit=limit)}


@server.tool(annotations=ToolAnnotations(title="Read an incident", read_only_hint=True), structured_output=True)
@_friendly
def get_incident(graph: str, incident: str) -> dict[str, Any]:
    """An incident's status, version, owner, alarms, affected services, cause, evidence and fix."""
    return _mem(mem.get_incident, graph, incident)


@server.tool(annotations=ToolAnnotations(title="Take an incident to the next step"), structured_output=True)
@_friendly
def claim_incident(graph: str, incident: str, agent: str, from_status: str, to_status: str, version: int = -1,
                   note: str = "", session: str = "") -> dict[str, Any]:
    """Hand-off step: move the incident from `from_status` to the next status and become its owner. Pass the
    `version` you read with get_incident: if anything changed since, you get a conflict instead of a silent overwrite,
    and exactly one of several agents racing for the same incident succeeds."""
    v = _mem(mem.claim, graph, incident, agent, from_status, to_status, version=None if version < 0 else version,
             note=note, session=session, log_as="claim_incident")
    return {"incident": incident, "status": to_status, "owner": agent, "version": v}


@server.tool(annotations=ToolAnnotations(title="Record the cause, with cited connections"), structured_output=True)
@_friendly
def record_finding(graph: str, incident: str, agent: str, cause: str, reason: str, cited: list[dict[str, str]],
                   session: str = "") -> dict[str, Any]:
    """Investigator's conclusion. You must own the incident in `investigating`. cause: the service at fault.
    cited: the map connections your reasoning relies on, [{caller, callee}] (from blast_radius / failure_paths).
    Each must exist on the map and be live, or nothing is recorded; the stored evidence is the map's own file:line."""
    return _mem(mem.record_finding, graph, incident, agent, cause, reason, cited, session=session, log_as="record_finding")


@server.tool(annotations=ToolAnnotations(title="Resolve an incident with its fix"), structured_output=True)
@_friendly
def resolve_incident(graph: str, incident: str, agent: str, fix: str, worked: bool = True, runbook: str = "",
                     session: str = "") -> dict[str, Any]:
    """Close a diagnosed incident with the fix applied. runbook: a short reusable name for the fix
    (e.g. "restart payments"); its attempt and success counts update. worked=false records a failed attempt and
    leaves the incident diagnosed."""
    return _mem(mem.resolve, graph, incident, agent, fix, worked=worked, runbook=runbook, session=session,
                log_as="resolve_incident")


@server.tool(annotations=ToolAnnotations(title="Past incidents like this one", read_only_hint=True), structured_output=True)
@_friendly
def similar_incidents(graph: str, services: list[str], cause: str = "", limit: int = 5, exclude: str = "") -> dict[str, Any]:
    """Memory recall: resolved incidents with overlapping affected services (and a bonus for the same cause), with
    the fix used and how often it has worked. Empty means nothing similar is on record."""
    return {"similar": _mem(mem.similar_incidents, graph, services, cause=cause, limit=limit, exclude=exclude)}


@server.tool(annotations=ToolAnnotations(title="Incident timeline", read_only_hint=True), structured_output=True)
@_friendly
def incident_timeline(graph: str, incident: str) -> dict[str, Any]:
    """Every hand-off on an incident in order: who opened, claimed, diagnosed and resolved it, with notes."""
    return {"incident": incident, "events": _mem(mem.timeline, graph, incident)}


@server.tool(annotations=ToolAnnotations(title="Start an agent session"), structured_output=True)
@_friendly
def start_session(graph: str, agent: str, task: str = "") -> dict[str, Any]:
    """Begin a session whose steps are stored in the graph. Pass the returned id as `session` to incident tools so
    each call is recorded; later sessions (or other agents) can read it with agent_history."""
    return {"session": _mem(mem.start_session, graph, agent, task)}


@server.tool(annotations=ToolAnnotations(title="Note a step in your session"), structured_output=True)
@_friendly
def log_step(graph: str, session: str, tool: str, input: str = "", output: str = "", incident: str = "") -> dict[str, Any]:
    """Record a step yourself (e.g. a blast_radius call and what you concluded from it)."""
    return {"step": _mem(mem.log_step, graph, session, tool, input, output, incident=incident)}


@server.tool(annotations=ToolAnnotations(title="Past agent sessions", read_only_hint=True), structured_output=True)
@_friendly
def agent_history(graph: str, session: str = "", agent: str = "", incident: str = "", limit: int = 50) -> dict[str, Any]:
    """What was done before, newest first: one session, one agent's sessions, or everything done on one incident."""
    return {"steps": _mem(mem.history, graph, session=session, agent=agent, incident=incident, limit=limit)}


if __name__ == "__main__":
    server.run(transport="stdio")
