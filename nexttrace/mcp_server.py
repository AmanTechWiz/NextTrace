"""NextTrace MCP server: lets Claude Code (or any MCP client) map repos, read the maps, and fill gaps.

Mapping is the deterministic parser in nexttrace.mapper; no LLM runs here. The connecting agent can add what
the parser missed with add_connection, which only accepts a quote the server finds in the code.

Run:  uv run python -m nexttrace.mcp_server        (stdio)
Env:  FALKORDB_HOST (localhost), FALKORDB_PORT (6379)
"""
import functools
import os
import re
from pathlib import Path
from typing import Any

import redis
from falkordb import FalkorDB
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nexttrace import mapper

server = MCPServer(
    name="nexttrace",
    instructions=(
        "NextTrace keeps a graph of a system's services and who calls whom, built from real repos. "
        "Call map_repo once per repo (several repos can share one graph), then list_maps and service_calls to read it. "
        "Every connection carries evidence (file:line or traffic count): cite it instead of guessing. "
        "The parser misses some wiring (registries, message buses, generated clients). Use find_gaps to see where, "
        "read that code, and record each call you confirm with add_connection, quoting the exact line. "
        "Connections you add are stored as inferred, never as fact."
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
    return FalkorDB(host=HOST, port=PORT)


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
        summary = mapper.map_repo(repo, name, traces or [], host=HOST, port=PORT)
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
    """Services the parser may have mapped incompletely, with the folder to read.

    isolated: no known calls in or out. no_service_calls: calls only datastores or infra (a frontend or gateway
    here usually means missed calls). Read their code, then record each confirmed call with add_connection.
    """
    g = _db().select_graph(_graph_name(graph))
    rows = g.ro_query(GAPS).result_set
    if not rows:
        raise ToolError(f"graph {graph!r} has no services; run map_repo first")
    roots = dict(g.ro_query("MATCH (r:MapRun) RETURN r.repo, r.root ORDER BY r.run").result_set)
    isolated, no_service_calls = [], []
    for name, repo, src, outs, ins, calls_service in rows:
        item = {"service": name, "folder": str(Path(roots.get(repo, "")) / src) if src else "", "callers": ins}
        if outs == 0 and ins == 0:
            isolated.append(item)
        elif not calls_service:
            no_service_calls.append(item)
    return {"graph": graph, "services": len(rows), "isolated": isolated, "no_service_calls": no_service_calls}


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


if __name__ == "__main__":
    server.run(transport="stdio")
