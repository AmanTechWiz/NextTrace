"""NextTrace MCP server: lets Claude Code (or any MCP client) map repos and read the maps.

Mapping is the deterministic parser in nexttrace.mapper; no LLM runs here.

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
        "Every connection carries evidence (file:line or traffic count): cite it instead of guessing."
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


if __name__ == "__main__":
    server.run(transport="stdio")
