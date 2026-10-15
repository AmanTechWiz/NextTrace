"""The MCP server, tested the way Claude Code uses it: a separate process over stdio."""
import asyncio
import sys
from pathlib import Path

import pytest
from falkordb import FalkorDB
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent.parent
GRAPH = "test_mcp"


def session(steps, env=None):
    """Start the server, run `steps(session)` against it, return what it returns."""
    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "nexttrace.mcp_server"], cwd=str(ROOT), env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await steps(s)
    return asyncio.run(go())


def make(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


COMPOSE = "services:\n  web: {build: ./web}\n  api: {build: ./api}\n  db: {image: 'postgres:16'}\n"


@pytest.fixture
def clean():
    db = FalkorDB()
    for g in (GRAPH, "shop"):
        if g in db.list_graphs():
            db.select_graph(g).delete()
    yield
    for g in (GRAPH, "shop"):
        if g in db.list_graphs():
            db.select_graph(g).delete()


def test_tools_are_listed_with_read_only_hints():
    tools = session(lambda s: s.list_tools()).tools
    hints = {t.name: bool(t.annotations and t.annotations.read_only_hint) for t in tools}
    assert hints == {"map_repo": False, "list_maps": True, "service_calls": True}


def test_map_then_read_with_evidence(tmp_path, clean):
    repo = make(tmp_path / "shop", {"docker-compose.yml": COMPOSE,
                                    "web/app.js": 'fetch("http://api:8080/orders")\n',
                                    "api/main.py": 'DB = "postgres://db:5432/app"\n'})

    async def steps(s):
        first = await s.call_tool("map_repo", {"path": str(repo)})          # graph defaults to folder name
        again = await s.call_tool("map_repo", {"path": str(repo)})
        api = await s.call_tool("service_calls", {"graph": "shop", "service": "api"})
        maps = await s.call_tool("list_maps", {})
        return first, again, api, maps

    first, again, api, maps = session(steps)
    assert not first.is_error and first.structured_content["graph"] == "shop"
    assert (first.structured_content["services"], first.structured_content["calls"]) == (3, 2)
    assert again.structured_content["calls"] == 2 and again.structured_content["stale_calls"] == 0  # idempotent
    a = api.structured_content
    assert [c["service"] for c in a["calls"]] == ["db"] and [c["service"] for c in a["called_by"]] == ["web"]
    assert a["called_by"][0]["evidence"][0] == 'code | web/app.js:1 | fetch("http://api:8080/orders")'
    shop = next(m for m in maps.structured_content["result"] if m["graph"] == "shop")
    assert shop["services"] == 3 and shop["repos"][0]["repo"] == "shop"


def test_two_repos_one_graph_join_across_repos(tmp_path, clean):
    deploy = make(tmp_path / "deploy", {"docker-compose.yml": "services:\n  catalogue: {image: 'cat:1'}\n"})
    front = make(tmp_path / "front-end", {"Dockerfile": "FROM node", "api.js": 'u = "http://catalogue/items"\n'})

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(deploy), "graph": GRAPH})
        out = await s.call_tool("map_repo", {"path": str(front), "graph": GRAPH})
        cat = await s.call_tool("service_calls", {"graph": GRAPH, "service": "catalogue"})
        return out, cat

    out, cat = session(steps)
    assert out.structured_content["cross_repo_calls"] == 1
    assert cat.structured_content["called_by"][0]["service"] == "front-end"


def test_bad_inputs_give_clear_errors(tmp_path, clean):
    empty = make(tmp_path / "empty", {"README.md": "nothing here"})
    bad_csv = make(tmp_path / "t", {"spans.csv": "a,b\n1,2\n"}) / "spans.csv"
    make(tmp_path / "shop", {"docker-compose.yml": COMPOSE})

    async def steps(s):
        return [await s.call_tool(name, args) for name, args in [
            ("map_repo", {"path": str(tmp_path / "missing")}),
            ("map_repo", {"path": str(empty), "graph": "bad name!"}),
            ("map_repo", {"path": str(tmp_path / "shop"), "graph": GRAPH, "traces": [str(tmp_path / "nope.csv")]}),
            ("map_repo", {"path": str(tmp_path / "shop"), "graph": GRAPH, "traces": [str(bad_csv)]}),
            ("map_repo", {"path": str(empty), "graph": GRAPH}),
            ("map_repo", {"path": str(tmp_path / "shop"), "graph": GRAPH}),
            ("service_calls", {"graph": GRAPH, "service": "ap"}),
        ]]

    missing, bad_name, no_traces, bad_traces, nothing, _, typo = session(steps)
    assert missing.is_error and "not a folder" in missing.content[0].text
    assert bad_name.is_error and "graph name" in bad_name.content[0].text
    assert no_traces.is_error and "traces file not found" in no_traces.content[0].text
    assert bad_traces.is_error and "could not read traces" in bad_traces.content[0].text
    assert not nothing.is_error and nothing.structured_content["services"] == 0 and "hint" in nothing.structured_content
    assert typo.is_error and "did you mean ['api']" in typo.content[0].text


def test_database_down_gives_actionable_error(tmp_path):
    repo = make(tmp_path / "shop", {"docker-compose.yml": COMPOSE})
    env = {"FALKORDB_PORT": "6390", "PATH": "/usr/bin:/bin"}  # nothing listens on 6390
    res = session(lambda s: s.call_tool("map_repo", {"path": str(repo)}), env=env)
    assert res.is_error and "not reachable at localhost:6390" in res.content[0].text
