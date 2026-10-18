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


def facts(g, files):
    touched, unowned = changed_services(g, files)
    names = sorted(touched)[:MAX_SERVICES]
    out = {"files": list(files), "unowned_files": unowned, "services": [], "connections": [], "routes": [],
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
            "dependents": rank["dependents"], "rank": rank["rank"], "of": of, "kind": b["kind"], "betweenness": rank["betweenness"],
            "pagerank": rank["pagerank"], "entry_points_hit": b["entry_points_hit"],
            "owners_to_notify": b["owners_to_notify"],
            "affected": [{"service": a["service"], "owner": a["owner"], "depth": a["depth"], "path": a["path"],
                          "confidence": a["confidence"], "cites": [cid(h) for h in a["hops"]]} for a in b["affected"]],
            # what it calls: a change can also break how it uses its own dependencies
            "calls": [{"service": callee, "cites": [cid({"caller": n, "callee": callee, "confidence": conf, "evidence": ev})]}
                      for callee, conf, ev in g.ro_query(
                          "MATCH (s:Service {name: $n})-[c:CALLS]->(t:Service) WHERE NOT coalesce(c.stale, false) "
                          "RETURN t.name, c.confidence, c.evidence[0] ORDER BY t.name", {"n": n}).result_set]})
    changed = set(files)
    for e_path, method, path, svc, callers in g.ro_query(
            "MATCH (s:Service)-[:EXPOSES]->(e:Endpoint) WHERE NOT coalesce(e.stale, false) "
            "OPTIONAL MATCH (c:Service)-[x:CALLS_ENDPOINT]->(e) WHERE NOT coalesce(x.stale, false) AND c <> s "
            "RETURN e.where, e.method, e.path, s.name, collect({caller: c.name, evidence: x.evidence[0]})").result_set:
        if (e_path or "").rsplit(":", 1)[0] in changed:
            callers = [c for c in callers if c.get("caller")]
            out["routes"].append({"id": f"R{len(out['routes']) + 1}", "service": svc, "route": f"{method} {path}",
                                  "declared_at": e_path, "callers": callers})
    if names:
        for j, inc in enumerate(mem.similar_incidents(g, names, limit=5), 1):
            out["incidents"].append({"id": f"I{j}", **{k: inc[k] for k in
                                     ("incident", "title", "cause", "fix", "runbook", "worked", "services", "score")}})
    out["connections"] = list(conns.values())
    return out


def verdict(f):
    """Rules over the facts, with the reasons that triggered them."""
    why = []
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
- Prefer few, specific findings (at most 6) over many vague ones. Say what to check, where, and why.
- verdict: risky if the diff plausibly breaks a caller or repeats a past incident; review if dependents exist but the
  change looks compatible; safe if nothing depends on it. The summary is 2-3 plain sentences for the reviewer."""


def _compact(f):
    """The facts the model sees: small, and only what it may cite."""
    return {"services": [{k: s[k] for k in ("id", "service", "owner", "changed_files", "dependents", "rank",
                                            "of", "entry_points_hit", "calls")} | {
             "affected": [{"service": a["service"], "depth": a["depth"], "path": a["path"], "cites": a["cites"]}
                          for a in s["affected"][:40]]} for s in f["services"]],
            "calls": [{k: c[k] for k in ("id", "caller", "callee", "evidence")} for c in f["connections"][:120]],
            "changed_routes": [{"id": r["id"], "service": r["service"], "route": r["route"],
                                "callers": [c["caller"] for c in r["callers"]]} for r in f["routes"]],
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
           | {i["id"] for i in f["incidents"]})
    allowed = ({s["service"] for s in f["services"]} | {a["service"] for s in f["services"] for a in s["affected"]}
               | {c["service"] for s in f["services"] for c in s["calls"]}
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
    model = model or os.environ.get("NEXTTRACE_GEMINI_MODEL", "gemini-flash-latest")
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


def _mermaid(f, limit=40):
    def node(n):
        return re.sub(r"\W", "_", n)
    edges = sorted({(c["caller"], c["callee"]) for c in f["connections"]})
    names = sorted({x for e in edges for x in e} | {s["service"] for s in f["services"]})
    lines = ["```mermaid", "graph LR"] + [f'  {node(n)}["{n}"]' for n in names]
    lines += [f"  {node(a)} --> {node(b)}" for a, b in edges[:limit]]
    lines += [f"  style {node(s['service'])} fill:#fde2e2,stroke:#c0392b,stroke-width:2px" for s in f["services"]]
    if len(edges) > limit:
        lines.append(f"  more[\"+{len(edges) - limit} more calls\"]")
    return lines + ["```"]


def render(f, rule_v, reasons, review=None, report=None, repo_url="", sha=""):
    v = review["verdict"] if review else rule_v
    L = [MARKER, f"## NextTrace: {BADGE[v]}", ""]
    if review and review["summary"]:
        L += [f"> {review['summary']}", ""]
    if not f["services"]:
        L += ["This change touches no service on the map, so nothing downstream is affected.", ""]
    else:
        L += ["| Changed service | Owner | Depends on it | Criticality | Entry points hit |", "|---|---|---|---|---|"]
        for s in f["services"]:
            L.append(f"| `{s['service']}` ({s['id']}) | {s['owner'] or '-'} | {s['dependents']} | "
                     f"#{s['rank']} of {s['of']} {s['kind']}s | {', '.join(s['entry_points_hit']) or '-'} |")
        L.append("")
    if review and review["findings"]:
        L += ["### Findings", ""]
        for x in review["findings"]:
            L.append(f"- **{x['severity'].upper()}: {x['title']}** {x['detail']} " + " ".join(f"`{c}`" for c in x["cites"]))
        L.append("")
    for r in f["routes"]:
        if r["callers"]:
            L += [f"### Changed route `{r['route']}` ({r['id']}) is called by", ""]
            L += [f"- `{c['caller']}` at {_link(c.get('evidence', ''), repo_url, sha)}" for c in r["callers"]] + [""]
    affected = [(s, a) for s in f["services"] for a in s["affected"]]
    if affected:
        L += ["### What this change can break", "", "| Service | Hops | Chain | Owner | Confidence |", "|---|---|---|---|---|"]
        for s, a in affected[:30]:
            L.append(f"| `{a['service']}` | {a['depth']} | {' → '.join(a['path'])} | {a['owner'] or '-'} | {a['confidence']} |")
        if len(affected) > 30:
            L.append(f"| … {len(affected) - 30} more | | | | |")
        L += [""] + _mermaid(f) + [""]
    if f["incidents"]:
        L += ["### Past incidents on these services", ""]
        L += [f"- **{i['incident']}** ({i['id']}): {i['title']}. Cause `{i['cause']}`; fix: {i['fix'] or '-'}"
              + (f" (runbook worked {i['worked']})" if i["worked"] else "") for i in f["incidents"]] + [""]
    owners = sorted({o for s in f["services"] for o in s["owners_to_notify"]})
    if owners:
        L += [f"**Suggested reviewers:** {', '.join(owners)}", ""]
    if review and review["checklist"]:
        L += ["### Before merging", ""] + [f"- [ ] {c}" for c in review["checklist"]] + [""]
    L += ["<details><summary>Why this verdict, and the evidence</summary>", "", "**Rules:** " + "; ".join(reasons), ""]
    for c in f["connections"]:
        L.append(f"- `{c['id']}` {c['caller']} → {c['callee']} ({c['confidence']}): {_link(c['evidence'], repo_url, sha)}")
    for s in f["services"]:
        L.append(f"- `{s['id']}` {s['service']}: changed {', '.join(f'`{x}`' for x in s['changed_files'][:5])}")
    if f["unowned_files"]:
        L.append(f"- Not in any mapped service: {', '.join(f'`{x}`' for x in f['unowned_files'][:10])}")
    L += ["", "</details>", ""]
    if review:
        note = f"AI review by `{report['model']}`: {report['kept']} finding(s) kept"
        if report["dropped"]:
            note += f", {len(report['dropped'])} dropped (" + "; ".join(f"{d['title']}: {d['why']}" for d in report["dropped"]) + ")"
        if review["ai_verdict"] != review["verdict"]:
            note += f". The AI said {review['ai_verdict']}; the rules set the floor at {review['verdict']}"
        L.append(f"<sub>{note}. Every fact comes from the NextTrace service map in FalkorDB.</sub>")
    else:
        L.append(f"<sub>Rule-based review ({(report or 'no AI')}). Every fact comes from the NextTrace service map in FalkorDB.</sub>")
    return "\n".join(L) + "\n"


# ---------- CLI ----------

def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def review_change(g, files, diff, repo_url="", sha="", use_ai=True):
    f = facts(g, files)
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
    root = Path(a.repo).resolve()
    if a.map:
        mapper.map_repo(root, a.graph)
    files = [x for x in _git(root, "diff", "--name-only", f"{a.base}...{a.head}").splitlines() if x]
    diff = _git(root, "diff", f"{a.base}...{a.head}")
    sha = a.sha or _git(root, "rev-parse", a.head).strip()
    comment, result = review_change(connect().select_graph(a.graph), files, diff, a.repo_url, sha, not a.no_ai)
    if a.out:
        Path(a.out).write_text(comment)
    else:
        sys.stdout.write(comment)
    print(json.dumps(result), file=sys.stderr)


if __name__ == "__main__":
    main()
