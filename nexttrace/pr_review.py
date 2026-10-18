"""PR review from the service map: what a change can break, with every claim cited to the graph.

  facts       deterministic, from FalkorDB: changed files -> services -> blast radius, criticality rank, changed routes
              other services call, past incidents. Every fact has an id (S1, C1, R1, I1) that points at stored evidence.
  verdict     fixed rules over the facts: safe / review / risky. Always computed, needs no AI.
  AI review   optional (GEMINI_API_KEY): one Gemini request gets the facts and the diff and returns a structured review
              that must cite fact ids. Findings with no valid citation, or naming services the facts don't contain, are
              dropped; kept citations are re-checked live in FalkorDB. The rules set the floor: the AI can raise the
              verdict, never lower it. No key, no budget left or an API error -> the rule-based comment is posted.
  key         bring your own: GEMINI_API_KEY is read only here, for PR reviews. Every other AI task in NextTrace runs in
              the user's own agent harness (Claude Code etc.) through the MCP tools; nothing else calls a model.
  rate limit  optional local cap for free-tier keys: NEXTTRACE_GEMINI_RPM / NEXTTRACE_GEMINI_RPD (unset = no cap), tracked
              in ~/.cache/nexttrace/gemini_calls.json. 429s are retried with the server's delay either way.

  uv run python -m nexttrace.pr_review --repo PATH --base origin/main --graph pr [--map] [--out comment.md]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from nexttrace import failure
from nexttrace import memory as mem

MARKER = "<!-- nexttrace-pr-review -->"
LEVEL = {"safe": 0, "review": 1, "risky": 2}
BADGE = {"safe": "🟢 Safe", "review": "🟡 Review", "risky": "🔴 Risky"}
MAX_SERVICES, MAX_DIFF_CHARS = 10, 60_000
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LEDGER = Path.home() / ".cache" / "nexttrace" / "gemini_calls.json"


# ---------- facts (graph only) ----------

def changed_services(g, files):
    """{service: [changed files]} by the service's code folder or the manifest that declares it; plus files no
    service owns (docs, shared libraries, CI)."""
    rows = g.ro_query("MATCH (s:Service) WHERE NOT coalesce(s.stale, false) "
                      "RETURN s.name, coalesce(s.source_dir, ''), coalesce(s.declared_in, '')").result_set
    hit, unowned = {}, []
    for f in files:
        owners = [n for n, d, decl in rows if (d and (f == d or f.startswith(d.rstrip("/") + "/"))) or f == decl]
        if not owners:
            unowned.append(f)
        # a file inside nested folders belongs to the deepest one (services/api/x.py -> api, not services)
        best = max((len(d) for n, d, _ in rows if n in owners), default=0)
        for n, d, decl in rows:
            if n in owners and (len(d) == best or f == decl):
                hit.setdefault(n, []).append(f)
    return hit, unowned


def _in_diff(diff, path, sign):
    """Does the diff add (+) or delete (-) a line holding this route's last literal segment?"""
    seg = next((x for x in reversed(path.split("/")) if x and "{" not in x and ":" not in x), "")
    return bool(seg) and any(l.startswith(sign) and not l.startswith(sign * 3) and seg in l for l in diff.splitlines())


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FUNC = [re.compile(p) for p in (
    r"^\s*(?:async\s+)?def\s+(\w+)",                                         # Python
    r"^func\s+(?:\([^)]*\)\s*)?(\w+)",                                        # Go
    r"\bfunction\s*\*?\s*(\w+)\s*\(",                                           # JS/TS
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>",  # JS/TS arrow
    r"^\s*(?:(?:public|private|protected|internal|static|final|override|abstract|async|virtual|synchronized|suspend|fun)"
    r"\s+)+[\w<>\[\],.?\s]*?\b(\w+)\s*\(",                                       # Java/C#/Kotlin method
    r"^\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{",                                  # JS/TS class method
)]
NOT_FUNC = {"if", "for", "while", "switch", "catch", "return", "new", "else", "try", "synchronized", "function"}


def changed_lines(diff, added_only=False):
    """{file: sorted new-side line numbers the diff adds, plus (unless added_only) the line just above each deletion}"""
    out, file, n = {}, None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = line[6:] if line.startswith("+++ b/") else None
        elif m := HUNK.match(line):
            n = int(m.group(1))
        elif file and line.startswith("+"):
            out.setdefault(file, set()).add(n)
            n += 1
        elif file and line.startswith("-") and not added_only:
            out.setdefault(file, set()).add(max(n - 1, 1))  # a deletion sits after the line above it
        elif file and line.startswith(" "):
            n += 1
    return {f: sorted(v) for f, v in out.items()}


def removed_functions(diff):
    """{file: functions whose signature line the diff deletes and does not add back}"""
    gone, back, file = {}, {}, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = line[6:] if line.startswith("+++ b/") else None
        elif file and line[:1] in "+-" and not line.startswith(("+++", "---")):
            m = next((m for p in FUNC if (m := p.search(line[1:]))), None)
            if m and m.group(1) not in NOT_FUNC:
                (gone if line[0] == "-" else back).setdefault(file, set()).add(m.group(1))
    return {f: sorted(v - back.get(f, set())) for f, v in gone.items() if v - back.get(f, set())}


def _read(root, rel):
    try:
        return (Path(root) / rel).read_text(errors="ignore").splitlines() if root else []
    except OSError:
        return []


def enclosing_functions(text, lines, limit=6):
    """Names of the functions/methods holding these line numbers (nearest signature above each line)."""
    names = []
    for n in lines:
        start = min(n, len(text)) - 1
        while 0 <= start < len(text) - 1 and text[start].lstrip().startswith("@"):  # decorator/annotation: its function is below
            start += 1
        for i in range(start, -1, -1):
            m = next((m for p in FUNC if (m := p.search(text[i]))), None)
            if m and m.group(1) not in NOT_FUNC:
                if m.group(1) not in names:
                    names.append(m.group(1))
                break
    return names[:limit]


def _snippet(root, evidence, around=3):
    """The caller's code around an evidence line ('kind | path:line | quote'), for the reviewer and the model."""
    parts = (evidence or "").split(" | ", 2)
    if len(parts) < 2 or ":" not in parts[1]:
        return ""
    path, line = parts[1].rsplit(":", 1)
    text = _read(root, path)
    if not text or not line.isdigit():
        return ""
    n = int(line)
    return "\n".join(text[max(0, n - 1 - around):n + around])


def facts(g, files, diff="", root=None, base=None):
    """base: (calls that matched a route before the change, all calls before), from a map of the base branch.
    With it, "broken by this PR" is exact (worked before, fails now, or a new call); without it, a diff heuristic."""
    touched, unowned = changed_services(g, files)
    if root is None:
        r = g.ro_query("MATCH (r:MapRun) RETURN r.root ORDER BY r.run DESC LIMIT 1").result_set
        root = r[0][0] if r else None
    lines_by_file = changed_lines(diff)
    added_by_file, removed = changed_lines(diff, added_only=True), removed_functions(diff)
    names = sorted(touched)[:MAX_SERVICES]
    out = {"files": list(files), "unowned_files": unowned, "services": [], "connections": [], "routes": [], "broken": [],
           "incidents": [], "truncated_services": max(0, len(touched) - MAX_SERVICES)}
    conns = {}

    def cid(h):
        key = (h["caller"], h["callee"])
        if key not in conns:
            conns[key] = {"id": f"C{len(conns) + 1}", "caller": h["caller"], "callee": h["callee"],
                          "confidence": h.get("confidence") or "", "evidence": h.get("evidence") or ""}
        return conns[key]["id"]

    for i, n in enumerate(names, 1):
        b = failure.blast_radius(g, n)
        rank = failure.criticality(g, service=n, kind=b["kind"] or "")["ranked"][0]
        of = g.ro_query("MATCH (s:Service {kind: $k}) WHERE NOT coalesce(s.stale, false) RETURN count(s)",
                        {"k": b["kind"]}).result_set[0][0]
        out["services"].append({
            "id": f"S{i}", "service": n, "owner": b["owner"], "changed_files": touched[n],
            "changed_functions": {f: fn for f in touched[n]
                                  if (fn := enclosing_functions(_read(root, f), added_by_file.get(f, [])))},
            "removed_functions": {f: removed[f] for f in touched[n] if f in removed},
            "dependents": rank["dependents"], "rank": rank["rank"], "of": of, "kind": b["kind"], "betweenness": rank["betweenness"],
            "pagerank": rank["pagerank"], "entry_points_hit": b["entry_points_hit"],
            "owners_to_notify": b["owners_to_notify"],
            # how each direct caller uses it: lets the reviewer (and the model) judge a contract change
            "caller_code": [{"caller": a["service"], "code": _snippet(root, a["hops"][0]["evidence"])}
                            for a in b["affected"] if a["depth"] == 1][:8],
            "affected": [{"service": a["service"], "owner": a["owner"], "depth": a["depth"], "path": a["path"],
                          "confidence": a["confidence"], "cites": [cid(h) for h in a["hops"]]} for a in b["affected"]],
            # what it calls: a change can also break how it uses its own dependencies
            "calls": [{"service": callee, "cites": [cid({"caller": n, "callee": callee, "confidence": conf, "evidence": ev})]}
                      for callee, conf, ev in g.ro_query(
                          "MATCH (s:Service {name: $n})-[c:CALLS]->(t:Service) WHERE NOT coalesce(c.stale, false) "
                          "RETURN t.name, c.confidence, c.evidence[0] ORDER BY t.name", {"n": n}).result_set]})
    # a route is changed when a changed line falls in its handler: from its declaration to the next route in the file
    declared = {}
    for e_path, method, path, svc, callers in g.ro_query(
            "MATCH (s:Service)-[:EXPOSES]->(e:Endpoint) WHERE NOT coalesce(e.stale, false) AND e.where IS NOT NULL "
            "OPTIONAL MATCH (c:Service)-[x:CALLS_ENDPOINT]->(e) WHERE NOT coalesce(x.stale, false) AND c <> s "
            "RETURN e.where, e.method, e.path, s.name, collect({caller: c.name, evidence: x.evidence[0]})").result_set:
        f, _, at = e_path.rpartition(":")
        if f in lines_by_file and at.isdigit():
            declared.setdefault(f, []).append((int(at), method, path, svc, [c for c in callers if c.get("caller")]))
    for f, rs in declared.items():
        rs.sort()
        for k, (at, method, path, svc, callers) in enumerate(rs):
            end = rs[k + 1][0] - 1 if k + 1 < len(rs) else 10 ** 9
            if any(at - 1 <= n <= end for n in added_by_file.get(f, [])):
                out["routes"].append({"id": f"R{len(out['routes']) + 1}", "service": svc, "route": f"{method} {path}",
                                      "declared_at": f"{f}:{at}", "callers": callers,
                                      "functions": enclosing_functions(_read(root, f), [at + 1], 1)})
    # calls that match no declared route, into or out of a changed service: a removed or renamed route, a new call to
    # a route that doesn't exist, or an older mismatch
    for caller, method, path, svc, ev in g.ro_query(
            "MATCH (c:Service)-[x:CALLS_ENDPOINT]->(e:Endpoint) WHERE (e.service IN $s OR c.name IN $s) "
            # undeclared on a fresh map; stale when a mapped-before route vanished
            "AND (NOT coalesce(e.declared, true) OR coalesce(e.stale, false)) "
            "AND NOT coalesce(x.stale, false) RETURN c.name, e.method, e.path, e.service, x.evidence[0] "
            "ORDER BY e.service, e.path, c.name", {"s": names}).result_set:
        out["broken"].append({"id": f"B{len(out['broken']) + 1}", "service": svc, "caller": caller,
                              "route": f"{method} {path}".strip(), "evidence": ev or "", "code": _snippet(root, ev),
                              # this PR's doing: it removed or moved the route, or it added the call
                              "new": ((caller, svc, method, path) in base[0] or (caller, svc, method, path) not in base[1])
                                     if base else
                                     (svc in touched and _in_diff(diff, path, "-"))
                                     or (caller in touched and _in_diff(diff, path, "+"))})
    if names:
        for j, inc in enumerate(mem.similar_incidents(g, names, limit=5), 1):
            out["incidents"].append({"id": f"I{j}", **{k: inc[k] for k in
                                     ("incident", "title", "cause", "fix", "runbook", "worked", "services", "score")}})
    for r in out["routes"]:
        for c in r["callers"]:
            c["code"] = _snippet(root, c.get("evidence"))
    out["connections"] = list(conns.values())
    return out


def verdict(f):
    """Rules over the facts, with the reasons that triggered them."""
    why = []
    for b in f["broken"]:
        why.append(("risky", f"{b['caller']} calls {b['route']} on {b['service']}, which does not exist after this change ({b['id']})")
                   if b["new"] else
                   ("review", f"{b['caller']} calls {b['route']} on {b['service']}, which it does not declare ({b['id']})"))
    for r in f["routes"]:
        if r["callers"]:
            why.append(("risky", f"{r['route']} ({r['id']}) changed and {len(r['callers'])} other service(s) call it"))
    for i in f["incidents"]:
        if i["cause"] in {s["service"] for s in f["services"]}:
            why.append(("risky", f"{i['cause']} caused past incident {i['incident']} ({i['id']})"))
    for s in f["services"]:
        if s["dependents"] and s["rank"] <= 3:
            why.append(("risky", f"{s['service']} is #{s['rank']} of {s['of']} by dependents ({s['id']})"))
        elif s["dependents"]:
            why.append(("review", f"{s['dependents']} service(s) depend on {s['service']} ({s['id']})"))
    level = max((LEVEL[v] for v, _ in why), default=0)
    if not f["services"]:
        why.append(("safe", "no mapped service is touched by this change"))
    elif level == 0:
        why.append(("safe", "nothing on the map depends on the touched services"))
    return [k for k, v in LEVEL.items() if v == level][0], [w for _, w in why]


# ---------- AI review (Gemini) ----------

SCHEMA = {"type": "OBJECT", "required": ["verdict", "summary", "findings"], "properties": {
    "verdict": {"type": "STRING", "enum": ["safe", "review", "risky"]},
    "summary": {"type": "STRING"},
    "findings": {"type": "ARRAY", "items": {"type": "OBJECT", "required": ["severity", "title", "detail", "cites"],
                 "properties": {"severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
                                "title": {"type": "STRING"}, "detail": {"type": "STRING"},
                                "files": {"type": "ARRAY", "items": {"type": "STRING"}},
                                "cites": {"type": "ARRAY", "items": {"type": "STRING"}}}}},
    "reviewer_checklist": {"type": "ARRAY", "items": {"type": "STRING"}}}}

SYSTEM = """You review pull requests for cross-service impact. You are given FACTS from a verified service map
(each with an id: S = changed service, C = a call between services with code evidence, R = a changed route other
services call, I = a past incident) and the DIFF. Judge what the change actually does (a rename, a new field, a changed
response, a removed route) and what that means for the services that depend on it.
Rules:
- Every finding must cite the fact ids it relies on in `cites`. A finding without facts behind it will be discarded.
- Name only services that appear in FACTS. Never invent services, calls, files or incidents.
- Read the callers' code: judge whether the diff actually breaks how they call (path, method, fields, response shape).
- Findings: at most 4, each 1-2 sentences, naming the file or function. No finding is better than a vague one.
  For a safe or trivial change return no findings.
- calls_to_missing_routes with new=true worked before this change and fail after it (or are new calls to a route that
  does not exist): say so plainly, naming the callers. direct_callers_code shows how callers use a changed service.
- verdict: risky if the diff plausibly breaks a caller or repeats a past incident; review if dependents exist but the
  change looks compatible; safe if nothing depends on it.
- summary: 1-3 plain sentences a busy reviewer reads first, like "This renames X in Y (File.java). Z and W still call
  the old path, so seat booking from the travel planner will fail." Say what changed, who is affected and what to do.
  No markdown tables, no fact ids in the summary.
- reviewer_checklist: at most 4 short, concrete actions; empty when nothing needs checking."""


def _compact(f):
    """The facts the model sees: small, and only what it may cite."""
    return {"services": [{k: s[k] for k in ("id", "service", "owner", "changed_files", "dependents", "rank",
                                            "of", "entry_points_hit", "calls")} | {
             "affected": [{"service": a["service"], "depth": a["depth"], "path": a["path"], "cites": a["cites"]}
                          for a in s["affected"][:40]]} for s in f["services"]],
            "calls": [{k: c[k] for k in ("id", "caller", "callee", "evidence")} for c in f["connections"][:120]],
            "changed_functions": {s["service"]: s["changed_functions"] for s in f["services"] if s["changed_functions"]},
            "removed_functions": {s["service"]: s["removed_functions"] for s in f["services"] if s["removed_functions"]},
            "direct_callers_code": {s["service"]: s["caller_code"] for s in f["services"] if s["caller_code"]},
            "calls_to_missing_routes": [{k: b[k] for k in ("id", "caller", "service", "route", "evidence", "new", "code")}
                                        for b in f["broken"]],
            "changed_routes": [{"id": r["id"], "service": r["service"], "route": r["route"], "handler": r["functions"],
                                "callers": [{"caller": c["caller"], "code": c.get("code", "")} for c in r["callers"]]}
                               for r in f["routes"]],
            "past_incidents": [{k: i[k] for k in ("id", "incident", "title", "cause", "fix", "worked")}
                               for i in f["incidents"]],
            "files_outside_any_service": f["unowned_files"][:50]}


def _rate_gate(rpm, rpd, ledger=LEDGER, sleep=time.sleep, now=time.time):
    """Block until a request fits the per-minute budget; raise when the daily budget is spent."""
    if not rpm and not rpd:
        return
    ledger.parent.mkdir(parents=True, exist_ok=True)
    t = now()
    calls = [c for c in (json.loads(ledger.read_text()) if ledger.exists() else []) if t - c < 86400]
    if rpd and len(calls) >= rpd:
        raise RuntimeError(f"daily Gemini budget spent ({rpd} requests in 24 h)")
    recent = sorted(c for c in calls if t - c < 60)
    if rpm and len(recent) >= rpm:
        sleep(60 - (t - recent[-rpm]) + 1)
    calls.append(now())
    ledger.write_text(json.dumps(calls))


def _post(model, key, body, timeout=120):
    req = urllib.request.Request(GEMINI_URL.format(model=model), data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def call_gemini(prompt, key, model, rpm=0, rpd=0, retries=2, post=_post, gate=_rate_gate):
    """One structured-output request, inside the rate budget; retries 429/5xx with the server's delay."""
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0.2}}
    for attempt in range(retries + 1):
        gate(rpm, rpd)
        try:
            resp = post(model, key, body)
            text = "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])
            return json.loads(text), resp.get("usageMetadata", {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code not in (429, 500, 503) or attempt == retries:
                raise RuntimeError(f"Gemini HTTP {e.code}: {detail}") from e
            m = re.search(r'"retryDelay":\s*"(\d+)', detail)
            time.sleep(int(m.group(1)) if m else 30)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Gemini returned no usable review: {e}") from e


def check_ai(g, f, ai, rule_verdict):
    """Keep only what the graph backs. Returns (clean review, report of what was dropped)."""
    ids = ({s["id"] for s in f["services"]} | {c["id"] for c in f["connections"]} | {r["id"] for r in f["routes"]}
           | {i["id"] for i in f["incidents"]} | {b["id"] for b in f["broken"]})
    allowed = ({s["service"] for s in f["services"]} | {a["service"] for s in f["services"] for a in s["affected"]}
               | {c["service"] for s in f["services"] for c in s["calls"]} | {b["caller"] for b in f["broken"]}
               | {c["caller"] for r in f["routes"] for c in r["callers"]} | {i["cause"] for i in f["incidents"]})
    known = {r[0] for r in g.ro_query("MATCH (s:Service) RETURN s.name").result_set}

    def strangers(text):  # map services the text names that the facts don't contain
        return sorted(n for n in known - allowed if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text or ""))

    kept, dropped = [], []
    for x in ai.get("findings", [])[:10]:
        cites = [c for c in x.get("cites", []) if c in ids]
        bad = strangers(f"{x.get('title', '')} {x.get('detail', '')}")
        if not cites:
            dropped.append({"title": x.get("title", ""), "why": "no valid citation"})
        elif bad:
            dropped.append({"title": x.get("title", ""), "why": f"names services not in the facts: {bad}"})
        else:
            kept.append({**x, "cites": cites})
    by_id = {c["id"]: c for c in f["connections"]}
    cited = [by_id[c] for x in kept for c in x["cites"] if c in by_id]
    live = failure.check_answer(g, [{"caller": c["caller"], "callee": c["callee"]} for c in cited]) if cited else {"ok": True}
    if not live["ok"]:  # a cited call vanished from the map between facts and posting
        gone = {(c["caller"], c["callee"]) for c in live["stale"] + live["not_on_map"]}
        kept = [x for x in kept if not any((by_id[c]["caller"], by_id[c]["callee"]) in gone
                                           for c in x["cites"] if c in by_id)]
    summary = ai.get("summary", "")
    if strangers(summary):
        dropped.append({"title": "summary", "why": f"names services not in the facts: {strangers(summary)}"})
        summary = ""
    checklist = [c for c in ai.get("reviewer_checklist", [])[:8] if not strangers(c)]
    ai_v = ai.get("verdict") if ai.get("verdict") in LEVEL else rule_verdict
    final = ai_v if LEVEL[ai_v] > LEVEL[rule_verdict] else rule_verdict
    return ({"verdict": final, "ai_verdict": ai_v, "summary": summary, "findings": kept, "checklist": checklist},
            {"kept": len(kept), "dropped": dropped})


def ai_review(g, f, diff, rule_verdict, key=None, model=None, call=call_gemini):
    """(clean review, report) or (None, reason) when the AI layer is off or fails."""
    key = key if key is not None else os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None, "no GEMINI_API_KEY: rule-based review only"
    if not f["services"]:
        return None, "no mapped service touched: nothing for the AI to assess"
    model = model or os.environ.get("NEXTTRACE_GEMINI_MODEL") or "gemini-flash-latest"  # CI passes unset vars as ""
    prompt = (f"FACTS:\n{json.dumps(_compact(f), indent=1)}\n\nRULE VERDICT: {rule_verdict}\n\n"
              f"DIFF (may be truncated):\n{diff[:MAX_DIFF_CHARS]}")
    try:
        ai, usage = call(prompt, key, model, rpm=int(os.environ.get("NEXTTRACE_GEMINI_RPM") or 0),
                         rpd=int(os.environ.get("NEXTTRACE_GEMINI_RPD") or 0))
    except Exception as e:  # any AI failure falls back to the rule-based review
        return None, f"AI review skipped: {e}"
    review, report = check_ai(g, f, ai, rule_verdict)
    report.update(model=model, tokens=usage.get("totalTokenCount"))
    return review, report


# ---------- comment ----------

def _link(evidence, repo_url, sha):
    """'code | path:line | quote' -> markdown link to the exact line (or the text when no URL is known)."""
    parts = (evidence or "").split(" | ", 2)
    if len(parts) < 2 or ":" not in parts[1]:
        return f"`{evidence[:80]}`" if evidence else ""
    path, line = parts[1].rsplit(":", 1)
    if repo_url and sha and line.isdigit():
        return f"[`{path}:{line}`]({repo_url}/blob/{sha}/{path}#L{line})"
    return f"`{path}:{line}`"


def _short(path):
    return path.rsplit("/", 1)[-1]


def _mermaid(f, max_nodes=14):
    """Small, readable: changed services, the services that break, and their direct callers. Dark text on light fills
    so it reads in both GitHub themes."""
    def node(n):
        return re.sub(r"\W", "_", n)
    changed = [s["service"] for s in f["services"]]
    broken = sorted({b["caller"] for b in f["broken"] if b["new"]})
    callers = sorted({c["caller"] for r in f["routes"] for c in r["callers"]}
                     | {a["service"] for s in f["services"] for a in s["affected"] if a["depth"] == 1})
    second = sorted({a["service"] for s in f["services"] for a in s["affected"] if a["depth"] == 2})
    names = list(dict.fromkeys(changed + broken + callers + second))[:max_nodes]
    keep = set(names)
    edges = sorted({(c["caller"], c["callee"]) for c in f["connections"] if c["caller"] in keep and c["callee"] in keep}
                   | {(b["caller"], b["service"]) for b in f["broken"] if b["caller"] in keep and b["service"] in keep})
    if not edges:
        return []
    L = ["```mermaid", "flowchart RL",
         "  classDef changed fill:#ffd6d6,stroke:#c62828,stroke-width:2px,color:#111111,font-weight:bold",
         "  classDef breaks fill:#ffe9b3,stroke:#e65100,stroke-width:2px,color:#111111",
         "  classDef caller fill:#dbeafe,stroke:#1d4ed8,stroke-width:1px,color:#111111",
         "  classDef far fill:#f1f5f9,stroke:#64748b,stroke-width:1px,color:#111111"]
    for n in names:
        L.append(f'  {node(n)}["{n}"]')
    broken_edges = {(b["caller"], b["service"]) for b in f["broken"] if b["new"]}
    for a, b in edges:
        L.append(f"  {node(a)} -. calls a removed route .-> {node(b)}" if (a, b) in broken_edges
                 else f"  {node(a)} --> {node(b)}")
    for cls, group in (("changed", changed), ("breaks", broken), ("caller", callers), ("far", second)):
        members = [node(n) for n in group if n in keep and (cls == "changed" or n not in changed)
                   and (cls != "caller" or n not in broken) and (cls != "far" or n not in broken + callers)]
        if members:
            L.append(f"  class {','.join(dict.fromkeys(members))} {cls}")
    if len(set(changed + broken + callers + second)) > max_nodes:
        L.append(f"  %% {len(set(changed + broken + callers + second)) - max_nodes} more in the table below")
    return L + ["```"]


def summary_text(f, rule_v):
    """The plain-language summary when there is no AI one: what changed, who breaks, how far it reaches."""
    if not f["services"]:
        return "No service on the map is touched by this change, so nothing downstream is affected."
    parts = []
    for s in f["services"]:
        fns = [f"`{fn}`" for v in s["changed_functions"].values() for fn in v][:3]
        fns += [f"removes `{fn}`" for v in s["removed_functions"].values() for fn in v][:3]
        files = ", ".join(f"`{_short(x)}`" for x in s["changed_files"][:3])
        parts.append(f"`{s['service']}` ({files}" + (f": {', '.join(fns)}" if fns else "") + ")")
    out = ["This PR changes " + "; ".join(parts) + "."]
    new = [b for b in f["broken"] if b["new"]]
    if new:
        routes = sorted({b["route"] for b in new})
        who = sorted({b["caller"] for b in new})
        out.append(f"**{len(who)} service{'s' if len(who) > 1 else ''} will break:** {', '.join(f'`{x}`' for x in who)} "
                   f"still call{'s' if len(who) == 1 else ''} {', '.join(f'`{r}`' for r in routes)}, which no longer exists.")
    for r in f["routes"]:
        if r["callers"]:
            who = ", ".join(f"`{c['caller']}`" for c in r["callers"][:4])
            out.append(f"`{r['route']}` changed and is used by {who}"
                       + (f" and {len(r['callers']) - 4} more" if len(r["callers"]) > 4 else "") + ".")
    reach = sum(s["dependents"] for s in f["services"])
    users = sorted({e for s in f["services"] for e in s["entry_points_hit"]})
    if reach:
        out.append(f"Up to {reach} service{'s' if reach > 1 else ''} depend on this"
                   + (f"; users reach it through {', '.join(f'`{u}`' for u in users[:3])}." if users else "."))
    elif not new:
        out.append("Nothing on the map depends on it.")
    return " ".join(out)


def _cite_link(cid, f, repo_url, sha):
    for b in f["broken"]:
        if b["id"] == cid:
            return _link(b["evidence"], repo_url, sha)
    for c in f["connections"]:
        if c["id"] == cid:
            return _link(c["evidence"], repo_url, sha)
    for r in f["routes"]:
        if r["id"] == cid:
            return _link(f"code | {r['declared_at']}", repo_url, sha)
    for i in f["incidents"]:
        if i["id"] == cid:
            return f"incident {i['incident']}"
    return ""


def render(f, rule_v, reasons, review=None, report=None, repo_url="", sha=""):
    v = review["verdict"] if review else rule_v
    new = [b for b in f["broken"] if b["new"]]
    headline = {"safe": "No cross-service impact", "review": "Has downstream dependents",
                "risky": f"Breaks {len({b['caller'] for b in new})} caller(s)" if new else "High-impact change"}[v]
    L = [MARKER, f"### {BADGE[v]} · {headline}", "",
         (review["summary"] if review and review["summary"] else summary_text(f, rule_v)), ""]
    if v == "safe" and not (review and review["findings"]):
        L.append(f"_NextTrace checked {len(f['files'])} changed file(s) against the service map._")
        return "\n".join(L) + "\n"
    if new:
        L += ["**Will break**", ""]
        for b in new:
            L.append(f"- `{b['caller']}` → `{b['route']}` at {_link(b['evidence'], repo_url, sha)}")
        L.append("")
    if review and review["findings"]:
        L += ["**Review notes**", ""]
        for x in review["findings"][:4]:
            where = next((l for c in x["cites"] if (l := _cite_link(c, f, repo_url, sha))), "")
            L.append(f"- **{x['title']}** {x['detail']}" + (f" ({where})" if where else ""))
        L.append("")
    if review and review["checklist"] and v != "safe":
        L += ["**Before merging**", ""] + [f"- [ ] {c}" for c in review["checklist"][:4]] + [""]
    L += _mermaid(f) + [""]
    if f["incidents"]:
        L += ["**Seen before**", ""]
        L += [f"- {i['incident']}: {i['title']} (cause `{i['cause']}`). Fix: {i['fix'] or '-'}"
              + (f", worked {i['worked']}" if i["worked"] else "") for i in f["incidents"][:3]] + [""]
    owners = sorted({o for s in f["services"] for o in s["owners_to_notify"]})
    if owners:
        L += [f"**Owners to loop in:** {', '.join(owners)}", ""]
    affected = [(s, a) for s in f["services"] for a in s["affected"]]
    L += ["<details><summary>Full impact, routes and evidence</summary>", ""]
    for s in f["services"]:
        L.append(f"- `{s['service']}`: {s['dependents']} dependent(s), #{s['rank']} of {s['of']} {s['kind']}s by impact")
    if affected:
        L += ["", "| Service | Hops | Via |", "|---|---|---|"]
        for s, a in affected[:40]:
            L.append(f"| `{a['service']}` | {a['depth']} | {' → '.join(a['path'][1:-1]) or 'direct'} |")
        if len(affected) > 40:
            L.append(f"| +{len(affected) - 40} more | | |")
    for r in f["routes"]:
        if r["callers"]:
            L += ["", f"`{r['route']}` is called from:"]
            L += [f"- `{c['caller']}` at {_link(c.get('evidence', ''), repo_url, sha)}" for c in r["callers"]]
    older = [b for b in f["broken"] if not b["new"]]
    if older:
        L += ["", "Already calling routes that don't exist (not caused by this PR):"]
        L += [f"- `{b['caller']}` → `{b['route']}` at {_link(b['evidence'], repo_url, sha)}" for b in older]
    if f["connections"]:
        L += ["", "Call evidence:"]
        L += [f"- {c['caller']} → {c['callee']}: {_link(c['evidence'], repo_url, sha)}" for c in f["connections"][:30]]
    if f["unowned_files"]:
        L += ["", "Files outside any mapped service: " + ", ".join(f"`{x}`" for x in f["unowned_files"][:10])]
    L += ["", "Rules: " + "; ".join(reasons), "", "</details>", ""]
    if review:
        note = f"Reviewed with `{report['model']}` on top of the service map"
        if report["dropped"]:
            note += f"; {len(report['dropped'])} AI note(s) dropped for lacking graph evidence"
        if review["ai_verdict"] != review["verdict"]:
            note += f"; rules kept the verdict at {review['verdict']} (AI said {review['ai_verdict']})"
    else:
        note = "Rule-based review from the service map" + (f" ({report})" if report and "GEMINI" not in str(report) else "")
    L.append(f"_{note}. Every service, call and line above comes from the NextTrace graph in FalkorDB._")
    return "\n".join(L) + "\n"


# ---------- CLI ----------

def load_env(path=".env"):
    """Read KEY=value lines from a .env file into the environment, without overriding what is already set."""
    f = Path(path)
    if not f.is_file():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip().removeprefix("export "), v.strip().strip("\"'"))


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def base_calls(root, base_ref, graph):
    """Map the base branch in a temporary worktree: (calls that matched a declared route, all calls)."""
    import shutil
    import tempfile
    from nexttrace import mapper
    from nexttrace.graphdb import connect
    tmp = Path(tempfile.mkdtemp(prefix="nexttrace-base-")) / "base"
    db = connect()
    _git(root, "worktree", "add", "--detach", "--quiet", str(tmp), base_ref)
    try:
        mapper.map_repo(tmp, graph)
        rows = db.select_graph(graph).ro_query(
            "MATCH (c:Service)-[x:CALLS_ENDPOINT]->(e:Endpoint) WHERE NOT coalesce(x.stale, false) "
            "RETURN c.name, e.service, e.method, e.path, coalesce(e.declared, false)").result_set
        return {tuple(r[:4]) for r in rows if r[4]}, {tuple(r[:4]) for r in rows}
    finally:
        if graph in db.list_graphs():
            db.select_graph(graph).delete()
        subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(tmp)], capture_output=True)
        shutil.rmtree(tmp.parent, ignore_errors=True)


def review_change(g, files, diff, repo_url="", sha="", use_ai=True, root=None, base=None):
    f = facts(g, files, diff, root, base)
    rule_v, reasons = verdict(f)
    review, report = ai_review(g, f, diff, rule_v) if use_ai else (None, "AI disabled")
    if review is None:
        return render(f, rule_v, reasons, None, report, repo_url, sha), {"verdict": rule_v, "ai": report}
    return render(f, rule_v, reasons, review, report, repo_url, sha), {"verdict": review["verdict"], "ai": report}


def main(argv=None):
    from nexttrace import mapper
    from nexttrace.graphdb import connect
    ap = argparse.ArgumentParser(description="Review a change against the NextTrace service map.")
    ap.add_argument("--repo", default=".", help="path to the git checkout")
    ap.add_argument("--base", required=True, help="git ref to compare against, e.g. origin/main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--graph", default="pr_review")
    ap.add_argument("--map", action="store_true", help="map the checkout into the graph first")
    ap.add_argument("--out", help="write the comment here (default: stdout)")
    ap.add_argument("--repo-url", default="", help="e.g. https://github.com/org/repo, for evidence links")
    ap.add_argument("--sha", default="", help="commit for evidence links (default: head)")
    ap.add_argument("--no-ai", action="store_true")
    a = ap.parse_args(argv)
    load_env()
    root = Path(a.repo).resolve()
    if a.map:
        mapper.map_repo(root, a.graph)
    files = [x for x in _git(root, "diff", "--name-only", f"{a.base}...{a.head}").splitlines() if x]
    diff = _git(root, "diff", f"{a.base}...{a.head}")
    sha = a.sha or _git(root, "rev-parse", a.head).strip()
    base = base_calls(root, a.base, f"{a.graph}__base") if a.map else None
    comment, result = review_change(connect().select_graph(a.graph), files, diff, a.repo_url, sha, not a.no_ai, root, base)
    if a.out:
        Path(a.out).write_text(comment)
    else:
        sys.stdout.write(comment)
    print(json.dumps(result), file=sys.stderr)


if __name__ == "__main__":
    main()
