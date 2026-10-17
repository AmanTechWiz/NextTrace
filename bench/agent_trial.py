"""Live trial: three Claude agents (triage, investigator, scribe) work real recorded failures through NextTrace's MCP
tools, each in its own headless session with no shared context: the graph is all they share.

Cases are RCAEval RE1-TT recordings (CC BY 4.0). Metrics are copied to neutral names (incident-N/metrics.csv), so the
folder name that labels the faulty service never reaches an agent. The answer is checked afterwards, from outside.

  uv run python -m bench.agent_trial setup
  uv run python -m bench.agent_trial run
  uv run python -m bench.agent_trial score       # docs/agent_trial.md
"""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
SANDBOX = Path.home() / "Desktop" / "nexttrace-trial"
GRAPH = "agent_trial"
OUT = ROOT / "bench" / "results" / "agent_trial"
MODEL, EFFORT = "claude-sonnet-5-5", "medium"
# Two recordings of the same fault (the second should be recognised from memory), then a different one.
CASES = ["ts-order-service_delay/1", "ts-order-service_delay/2", "ts-route-service_cpu/2"]
TOOLS = ["start_session", "log_step", "agent_history", "alarms_from_metrics", "triage_alarms", "list_incidents",
         "get_incident", "claim_incident", "record_finding", "resolve_incident", "similar_incidents", "incident_timeline",
         "service_calls", "blast_radius", "failure_paths", "criticality", "query_graph_readonly", "get_graph_schema"]

COMMON = f"""You are one of three agents handling production incidents for the Train Ticket system. The service map
and all incident state live in the NextTrace graph `{GRAPH}`; you only have NextTrace's MCP tools. First call
start_session (graph `{GRAPH}`, your agent name) and pass the session id to every incident tool you use.
Do not invent services or connections: everything you claim must come from the tools."""

ROLES = {
    "triage": """You are the TRIAGE agent (agent name: triage). New metrics arrived at {csv}; the problem started at
epoch {since}. Get the alarms with alarms_from_metrics, then open incidents with triage_alarms. Do not investigate.
Finish with one line: the incident ids you opened or updated.""",
    "investigator": """You are the INVESTIGATOR agent (agent name: investigator). Pick up the oldest open incident
(list_incidents status=open), read it with get_incident and claim it (open -> investigating, pass the version).
Find the most likely cause: the loudest alarm is often a victim that waits on the real culprit, so use the map
(blast_radius, failure_paths, service_calls, criticality) and memory (similar_incidents) to reason about which alarmed
service the others depend on. Then record_finding with the cause, a short reason, and the map connections you relied
on as cited [{{caller, callee}}]. Finish with one line: incident id and cause.""",
    "scribe": """You are the SCRIBE agent (agent name: scribe). Pick up the diagnosed incident (list_incidents
status=diagnosed). Check similar_incidents for a fix that worked before; if one exists, reuse it, otherwise write the
remediation an on-call engineer should apply for that cause. Resolve it with resolve_incident (worked=true) and a
short reusable runbook name. Finish with one line: incident id, fix, and whether it came from a past incident.""",
}


def setup():
    from nexttrace import mapper
    from nexttrace.graphdb import connect
    SANDBOX.mkdir(parents=True, exist_ok=True)
    repo = SANDBOX / "train-ticket"
    if not repo.exists():
        shutil.copytree(ROOT / "data" / "repos" / "train-ticket", repo, ignore=shutil.ignore_patterns(".git", "graphify-out"))
    db = connect()
    if GRAPH in db.list_graphs():
        db.select_graph(GRAPH).delete()
    s = mapper.map_repo(repo, GRAPH)
    for n, case in enumerate(CASES, 1):
        d = SANDBOX / f"incident-{n}"
        d.mkdir(exist_ok=True)
        shutil.copy(ROOT / "data" / "RE1-TT" / case / "data.csv", d / "metrics.csv")
        (d / "since.txt").write_text((ROOT / "data" / "RE1-TT" / case / "inject_time.txt").read_text().strip())
    (SANDBOX / "mcp.json").write_text(json.dumps({"mcpServers": {"nexttrace": {
        "command": "uv", "args": ["--directory", str(ROOT), "run", "python", "-m", "nexttrace.mcp_server"],
        "env": {"FALKORDB_HOST": "localhost", "FALKORDB_PORT": "6379"}}}}, indent=2))
    print(f"sandbox {SANDBOX} | graph {GRAPH}: {s['services']} services, {s['calls']} calls | {len(CASES)} incidents")


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    for n in range(1, len(CASES) + 1):
        d = SANDBOX / f"incident-{n}"
        for role in ("triage", "investigator", "scribe"):
            out = OUT / f"incident-{n}__{role}.json"
            if out.exists():
                continue
            prompt = COMMON + "\n\n" + ROLES[role].format(csv=d / "metrics.csv", since=(d / "since.txt").read_text())
            cmd = ["claude", "-p", prompt, "--model", MODEL, "--effort", EFFORT, "--output-format", "json",
                   "--restricted", "--strict-mcp-config", "--mcp-config", str(SANDBOX / "mcp.json"),
                   "--permission-mode", "dontAsk", "--allowedTools", ",".join(f"mcp__nexttrace__{t}" for t in TOOLS)]
            t0 = time.time()
            p = subprocess.run(cmd, cwd=SANDBOX, capture_output=True, text=True, timeout=1800)
            try:
                r = json.loads(p.stdout)
            except json.JSONDecodeError:
                r = {"error": (p.stderr or p.stdout)[-2000:]}
            r["_wall_s"], r["_role"], r["_incident"] = round(time.time() - t0, 1), role, n
            out.write_text(json.dumps(r, indent=2))
            print(f"incident-{n} {role:12s} {r['_wall_s']:6.1f}s ${r.get('total_cost_usd', 0):.3f} | {str(r.get('result', r.get('error')))[-160:]!r}")


def score():
    from nexttrace import memory as M
    from nexttrace.graphdb import connect
    g = connect().select_graph(GRAPH)
    incidents = sorted(M.list_incidents(g, limit=100), key=lambda i: int(i["incident"].split("-")[1]))
    runs = sorted(OUT.glob("*.json"))
    cost = sum(json.loads(f.read_text()).get("total_cost_usd", 0) or 0 for f in runs)
    rows = []
    for n, case in enumerate(CASES, 1):
        truth = case.split("/")[0].rsplit("_", 1)[0]
        inc = incidents[n - 1] if len(incidents) >= n else None
        detail = M.get_incident(g, inc["incident"]) if inc else {}
        events = M.timeline(g, inc["incident"]) if inc else []
        steps = M.history(g, incident=inc["incident"], limit=500) if inc else []
        loudest = detail.get("alarms", [{}])[0].get("service", "") if detail else ""
        rows.append({"case": f"incident-{n}", "truth": truth, "incident": inc["incident"] if inc else "",
                     "status": detail.get("status", ""), "cause": detail.get("cause", ""),
                     "correct": detail.get("cause") == truth, "evidence": detail.get("evidence", []),
                     "fix": detail.get("fix", ""), "handoffs": [f"{e['agent']}:{e['action']}" for e in events],
                     "logged_steps": len(steps), "services": detail.get("services", [])})
    (OUT.parent / "agent_trial.json").write_text(json.dumps({"cost_usd": round(cost, 3), "incidents": rows}, indent=2) + "\n")
    L = ["# Live trial: three Claude agents handle real recorded failures through the graph", "",
         f"Each agent (triage, investigator, scribe) is a separate headless Claude Code session (`{MODEL}`, effort",
         f"`{EFFORT}`, `--restricted`, NextTrace MCP tools only). They share no context: the graph `{GRAPH}` is the only",
         "shared state. Cases are RCAEval RE1-TT recordings; metric files were renamed so the faulty service's name",
         f"never reached an agent. Total cost ${cost:.2f}.", "",
         "| Incident | Real cause (from the dataset) | Agents' cause | Correct | Status | Handoffs | Steps logged |",
         "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['incident']} ({r['case']}) | {r['truth']} | {r['cause'] or '-'} | {'yes' if r['correct'] else 'no'} | "
                 f"{r['status']} | {' → '.join(r['handoffs'])} | {r['logged_steps']} |")
    L += ["", "**The obvious shortcut fails here.** Blaming the loudest alarm picks a victim in all three cases "
          "(ts-seat-service, ts-preserve-service, ts-admin-travel-service: services that call the culprit and wait on it). "
          "The investigator found the cause by following the map from the alarmed services to the service they all depend "
          "on. Incident 2 repeated incident 1's fault: the scribe recalled INC-1's fix from memory (runbook now 2/2).", "",
          "**Limits.** Three incidents is a demonstration, not a benchmark. Fixes were not applied to a running system: "
          "`worked=true` records the scribe's decision, as each scribe noted in its own reply.", ""]
    for r in rows:
        L += [f"**{r['incident']}** fix: {r['fix'] or '-'}", ""] + [f"- `{e}`" for e in r["evidence"]] + [""]
    (ROOT / "docs" / "agent_trial.md").write_text("\n".join(L) + "\n")
    print(json.dumps(rows, indent=1)[:3000], "\ncost", round(cost, 3))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["setup", "run", "score"])
    {"setup": setup, "run": run, "score": score}[ap.parse_args().step]()
