"""Benchmark: the same agent (Claude Code, Sonnet 5.5, medium effort) answering dependency questions about Train Ticket
with and without NextTrace.

  setups      plain: the repo and file tools (Read, Grep, Glob) only.
              nexttrace: the same, plus NextTrace's read-only MCP tools over a map of the same checkout.
  isolation   claude -p --restricted: no shell, no web, file tools confined to the sandbox; settings files ignored.
              The answer key never leaves this repo.
  questions   "which services call X" and "what breaks if X fails", for services with 3+ callers in recorded traffic.
  grading     (independent of NextTrace's map)
              - recall vs real traffic: callers seen in the RE2-TT traces (incomplete, so a lower bound)
              - made-up claims: every claimed service needs a citation (file, line, exact code) and the grader checks
                it against the checkout itself: the quote must be in that file near that line, inside the caller's
                folder, and name the target. Claims that fail are counted as made up.
              - cost, time and turns as reported by the CLI.

  uv run python -m bench.agent_bench setup     # sandbox at ~/Desktop/nexttrace-bench, graph bench_tt (no traces)
  uv run python -m bench.agent_bench run       # 2 setups x questions, raw results in bench/results/agent_bench/
  uv run python -m bench.agent_bench score     # docs/agent_bench.md
"""
import argparse
import glob
import json
import re
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).parent.parent
SANDBOX = Path.home() / "Desktop" / "nexttrace-bench"
REPO = SANDBOX / "train-ticket"
GRAPH = "bench_tt"
OUT = ROOT / "bench" / "results" / "agent_bench"
MODEL, EFFORT = "claude-sonnet-5-5", "medium"
READ_TOOLS = ["Read", "Grep", "Glob"]
MCP_TOOLS = [f"mcp__nexttrace__{t}" for t in ("list_maps", "service_calls", "endpoint_calls", "blast_radius",
                                              "failure_paths", "criticality", "query_graph_readonly", "get_graph_schema")]
CALLERS = ["ts-station-service", "ts-order-service", "ts-seat-service", "ts-ticketinfo-service", "ts-train-service",
           "ts-route-service"]
BLAST = ["ts-seat-service", "ts-route-service"]

FORMAT = """Every service you list must be backed by evidence from the code: the file, the line, and the exact code on that
line where the caller reaches the target (a URL, address or client call). Paths are relative to the repo root.
End your reply with exactly one JSON block and nothing after it:
```json
{"services": ["ts-...", "..."], "evidence": [{"caller": "ts-...", "callee": "ts-...", "file": "ts-.../src/...", "line": 123, "quote": "exact code"}]}
```
For "what breaks" questions, give one evidence entry per hop of each chain. List only services you have evidence for."""


def questions():
    qs = [{"id": f"callers-{x}", "kind": "callers", "target": x,
           "text": f"Which services call {x} directly?"} for x in CALLERS]
    qs += [{"id": f"blast-{x}", "kind": "blast", "target": x,
            "text": f"If {x} fails, which services break, directly or through other services that depend on it?"} for x in BLAST]
    return qs


def prompt(q, setup):
    where = ("You are in a sandbox holding a checkout of the Train Ticket microservices system in train-ticket/. "
             "Each ts-*-service folder is one service; services call each other over HTTP.")
    tools = ("" if setup == "plain" else
             f"\nYou also have the NextTrace MCP tools. Graph `{GRAPH}` is a map of this exact checkout: services, who "
             "calls whom, each call with file:line evidence. Use it, and check the code when needed.")
    return f"{where}{tools}\n\nQuestion: {q['text']}\n\n{FORMAT}"


# ---------- setup ----------

def setup():
    from nexttrace import mapper
    from nexttrace.graphdb import connect
    src = ROOT / "data" / "repos" / "train-ticket"
    if not REPO.exists():
        SANDBOX.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, REPO, ignore=shutil.ignore_patterns(".git", "graphify-out"))
    g = connect().select_graph(GRAPH)
    if GRAPH in connect().list_graphs():
        g.delete()
    s = mapper.map_repo(REPO, GRAPH)  # parser only: no traces, so the answer key can't leak into the map
    (SANDBOX / "mcp.json").write_text(json.dumps({"mcpServers": {"nexttrace": {
        "command": "uv", "args": ["--directory", str(ROOT), "run", "python", "-m", "nexttrace.mcp_server"],
        "env": {"FALKORDB_HOST": "localhost", "FALKORDB_PORT": "6379"}}}}, indent=2))
    (SANDBOX / "empty-mcp.json").write_text('{"mcpServers": {}}')
    print(f"sandbox {SANDBOX} | graph {GRAPH}: {s['services']} services, {s['calls']} calls")


# ---------- run ----------

def run(only=None, setups=("plain", "nexttrace")):
    OUT.mkdir(parents=True, exist_ok=True)
    for q in questions():
        if only and q["id"] not in only:
            continue
        for setup_name in setups:
            out = OUT / f"{setup_name}__{q['id']}.json"
            if out.exists():
                continue
            mcp = SANDBOX / ("mcp.json" if setup_name == "nexttrace" else "empty-mcp.json")
            tools = READ_TOOLS + (MCP_TOOLS if setup_name == "nexttrace" else [])
            cmd = ["claude", "-p", prompt(q, setup_name), "--model", MODEL, "--effort", EFFORT, "--output-format", "json",
                   "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp), "--permission-mode", "dontAsk",
                   "--allowedTools", ",".join(tools)]
            t0 = time.time()
            proc = subprocess.run(cmd, cwd=SANDBOX, capture_output=True, text=True, timeout=1800)
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError:
                result = {"error": proc.stderr[-2000:] or proc.stdout[-2000:]}
            result["_wall_s"] = round(time.time() - t0, 1)
            result["_question"], result["_setup"] = q, setup_name
            out.write_text(json.dumps(result, indent=2))
            print(f"{setup_name:9s} {q['id']:32s} {result['_wall_s']:7.1f}s  cost ${result.get('total_cost_usd', 0):.3f}")


# ---------- answer key (independent of the map) ----------

def traffic_edges():
    import pandas as pd
    edges = set()
    for f in glob.glob(str(ROOT / "data" / "traces" / "RE2-TT" / "*.csv")):
        df = pd.read_csv(f, usecols=["spanID", "parentSpanID", "serviceName"])
        parent = dict(zip(df["spanID"], df["serviceName"]))
        edges |= {(parent[p], s) for p, s in zip(df["parentSpanID"], df["serviceName"])
                  if p in parent and parent[p] != s}
    return edges


def expected(q, edges):
    callers = {}
    for a, b in edges:
        callers.setdefault(b, set()).add(a)
    if q["kind"] == "callers":
        return callers.get(q["target"], set())
    seen, todo = set(), deque([q["target"]])
    while todo:
        for c in callers.get(todo.popleft(), ()):
            if c != q["target"] and c not in seen:
                seen.add(c)
                todo.append(c)
    return seen


def parse_answer(text):
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.S)
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None


def check_citation(ev):
    """Mechanical check against the checkout: quote in that file near that line, inside the caller's folder, naming
    the callee. Same rule NextTrace's server applies to agent edits, run here on the grader's side."""
    from nexttrace.mapper import verify_quote
    try:
        file = str(ev["file"]).removeprefix("train-ticket/").lstrip("/")
        caller, callee = ev["caller"], ev["callee"]
        if not file.startswith(caller + "/"):
            return False, "file is not in the caller's folder"
        verify_quote(REPO, file, int(ev["line"]), str(ev["quote"]), callee, slack=5)
        return True, ""
    except (KeyError, TypeError, ValueError) as e:
        return False, str(e)[:120]


def tools_only(edges):
    """The same questions answered by NextTrace's graph alone, no model: what the PR bot and CI get on every change."""
    from nexttrace import failure
    from nexttrace.graphdb import connect
    g, rows = connect().select_graph(GRAPH), []
    for q in questions():
        t0 = time.perf_counter()
        r = failure.blast_radius(g, q["target"], max_depth=1 if q["kind"] == "callers" else 6)
        wall = time.perf_counter() - t0
        claimed = {a["service"] for a in r["affected"]}
        checks = []
        for c in r["cited_connections"]:
            _, where, quote = c["evidence"].split(" | ", 2)
            f, line = where.rsplit(":", 1)
            checks.append(check_citation({"caller": c["caller"], "callee": c["callee"], "file": f, "line": int(line), "quote": quote})[0])
        truth = expected(q, edges)
        rows.append({"setup": "graph only (no AI)", "question": q["id"], "kind": q["kind"], "parsed": True,
                     "claimed": sorted(claimed), "traffic_callers": sorted(truth), "traffic_found": len(claimed & truth),
                     "traffic_total": len(truth), "made_up": [], "invalid_citations": checks.count(False),
                     "citations": len(checks), "cost_usd": 0.0, "wall_s": round(wall, 4), "turns": 0, "error": ""})
    return rows


def score():
    edges = traffic_edges()
    rows = []
    for f in sorted(OUT.glob("*.json")):
        r = json.loads(f.read_text())
        q, setup_name = r["_question"], r["_setup"]
        ans = parse_answer(r.get("result", ""))
        truth = expected(q, edges)
        claimed = set(ans.get("services", [])) if ans else set()
        evidence = ans.get("evidence", []) if ans else []
        checks = [(e, *check_citation(e)) for e in evidence if isinstance(e, dict)]
        good_callers = {e["caller"] for e, ok, _ in checks if ok}
        if q["kind"] == "callers":
            supported = {e["caller"] for e, ok, _ in checks if ok and e.get("callee") == q["target"]}
        else:  # a service is supported if a chain of valid hops links it to the target
            hops = {(e["caller"], e["callee"]) for e, ok, _ in checks if ok}
            supported = expected({"kind": "blast", "target": q["target"]}, hops)
        rows.append({
            "setup": setup_name, "question": q["id"], "kind": q["kind"], "parsed": ans is not None,
            "claimed": sorted(claimed), "traffic_callers": sorted(truth),
            "traffic_found": len(claimed & truth), "traffic_total": len(truth),
            "made_up": sorted(claimed - supported), "invalid_citations": sum(1 for _, ok, _ in checks if not ok),
            "citations": len(checks), "cost_usd": r.get("total_cost_usd", 0), "wall_s": r["_wall_s"],
            "turns": r.get("num_turns", 0), "error": r.get("error", "") or ("" if r.get("subtype") == "success" else r.get("subtype", "")),
            "_unused": sorted(good_callers - claimed),
        })
    rows += tools_only(edges)
    (OUT.parent / "agent_bench.json").write_text(json.dumps(rows, indent=2) + "\n")
    (ROOT / "docs" / "agent_bench.md").write_text(render(rows))
    for s in ("plain", "nexttrace", "graph only (no AI)"):
        mine = [r for r in rows if r["setup"] == s]
        if mine:
            print(s, summary(mine))


def summary(rows):
    found, total = sum(r["traffic_found"] for r in rows), sum(r["traffic_total"] for r in rows)
    claims = sum(len(r["claimed"]) for r in rows)
    made_up = sum(len(r["made_up"]) for r in rows)
    return {"questions": len(rows), "traffic_recall": f"{found}/{total}", "claims": claims, "made_up": made_up,
            "invalid_citations": sum(r["invalid_citations"] for r in rows),
            "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 3),
            "minutes": round(sum(r["wall_s"] for r in rows) / 60, 1), "turns": sum(r["turns"] or 0 for r in rows),
            "unparsed": sum(1 for r in rows if not r["parsed"])}


def render(rows):
    L = ["# Agent benchmark: the same agent with and without NextTrace", "",
         f"Claude Code headless (`claude -p --restricted`), model `{MODEL}`, effort `{EFFORT}`, on a checkout of Train",
         "Ticket v0.2.1. Same prompt and file tools in both setups; the nexttrace setup also gets NextTrace's read-only",
         "MCP tools over a map of the same checkout, built by the parser alone (no traffic).", "",
         "- **Traffic recall**: callers seen in recorded real traffic (RCAEval RE2-TT) that the answer lists. Traffic",
         "  misses many real callers (the UI is not traced), so this is a lower bound, not a full answer key.",
         "- **Made up**: listed services without a valid citation. Each citation is checked against the checkout:",
         "  the quoted code must be in that file near that line, inside the caller's folder, and name the target.", "",
         "| Setup | Questions | Traffic recall | Claims | Made up | Invalid citations | Cost | Time | Turns |",
         "|---|---|---|---|---|---|---|---|---|"]
    for s in ("plain", "nexttrace", "graph only (no AI)"):
        mine = [r for r in rows if r["setup"] == s]
        if mine:
            m = summary(mine)
            t = f"{sum(r['wall_s'] for r in mine) * 1000:.0f} ms" if s.startswith("graph") else f"{m['minutes']} min"
            L.append(f"| {s} | {m['questions']} | {m['traffic_recall']} | {m['claims']} | {m['made_up']} | "
                     f"{m['invalid_citations']} | ${m['cost_usd']} | {t} | {m['turns']} |")
    L += ["", "**Reading this honestly.** Train Ticket writes every call as a literal URL in one repo, so a plain agent",
          "with grep answers direct-caller questions just as well: the easiest case for it. Its gap shows on chains",
          "(blast-ts-seat-service: 4 of 6 vs 6 of 6). The graph alone answers all 8 questions in milliseconds for $0, which is",
          "what the PR bot and CI get on every change. One graph citation is counted invalid: ts-voucher-service-v2 is a",
          "second deployment of the ts-voucher-service code, and the grader requires the caller's own folder name.",
          "Not tested here, where the code you can grep doesn't hold the answer: other repos, whole-system questions,",
          "and knowledge earlier agents verified.", "", "## Per question", "", "| Question | Setup | Traffic found | Claims | Made up | Cost | Time |",
          "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["question"], r["setup"])):
        L.append(f"| {r['question']} | {r['setup']} | {r['traffic_found']}/{r['traffic_total']} | {len(r['claimed'])} | "
                 f"{', '.join(r['made_up']) or '0'} | ${r['cost_usd'] or 0:.3f} | {r['wall_s']:.0f}s |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["setup", "run", "score"])
    ap.add_argument("--only", nargs="*", help="question ids")
    ap.add_argument("--setups", nargs="*", default=["plain", "nexttrace"])
    a = ap.parse_args()
    {"setup": setup, "run": lambda: run(a.only, a.setups), "score": score}[a.step]()
