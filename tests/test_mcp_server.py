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
    assert hints == {"map_repo": False, "list_maps": True, "service_calls": True, "endpoint_calls": True,
                     "find_gaps": True, "add_connection": False, "export_map": False,
                     "add_service": False, "merge_services": False, "set_service_folder": False,
                     "remove_service": False, "remove_connection": False, "list_graphs": True,
                     "get_graph_schema": True, "query_graph_readonly": True,
                     "blast_radius": True, "failure_paths": True, "criticality": True,
                     "alarms_from_metrics": True, "triage_alarms": False, "list_incidents": True, "get_incident": True,
                     "claim_incident": False, "record_finding": False, "resolve_incident": False,
                     "similar_incidents": True, "incident_timeline": True, "start_session": False, "log_step": False,
                     "agent_history": True, "check_answer": True}


def test_map_then_read_with_evidence(tmp_path, clean):
    repo = make(tmp_path / "shop", {"docker-compose.yml": COMPOSE,
                                    "web/app.js": 'fetch("http://api:8080/orders", {method: "POST"})\n',
                                    "api/routes.py": "@app.post('/orders')\ndef create(): ...\n",
                                    "api/main.py": 'DB = "postgres://db:5432/app"\n'})

    async def steps(s):
        first = await s.call_tool("map_repo", {"path": str(repo)})          # graph defaults to folder name
        again = await s.call_tool("map_repo", {"path": str(repo)})
        api = await s.call_tool("service_calls", {"graph": "shop", "service": "api"})
        maps = await s.call_tool("list_maps", {})
        routes = await s.call_tool("endpoint_calls", {"graph": "shop", "service": "api"})
        return first, again, api, maps, routes

    first, again, api, maps, routes = session(steps)
    (route,) = routes.structured_content["exposes"]
    assert (route["method"], route["path"], route["where"]) == ("POST", "/orders", "api/routes.py:1")
    assert route["called_by"][0]["service"] == "web" and "web/app.js:1" in route["called_by"][0]["evidence"][0]
    assert not first.is_error and first.structured_content["graph"] == "shop"
    assert (first.structured_content["services"], first.structured_content["calls"]) == (3, 2)
    assert again.structured_content["calls"] == 2 and again.structured_content["stale_calls"] == 0  # idempotent
    a = api.structured_content
    assert [c["service"] for c in a["calls"]] == ["db"] and [c["service"] for c in a["called_by"]] == ["web"]
    assert a["called_by"][0]["evidence"][0] == 'code | web/app.js:1 | fetch("http://api:8080/orders", {method: "POST"})'
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


REGISTRY_REPO = {
    "docker-compose.yml": "services:\n  gateway: {build: ./gateway}\n  orders: {build: ./orders}\n  pay: {build: ./pay}\n",
    # wired through a registry: no address the parser can read
    "gateway/Routes.java": "route(\"/orders/**\").to(Service.ORDERS);\n",
    "orders/Client.java": "class C {\n  // calls payment through the registry\n  Response r = lb.call(Service.PAY, \"/charge\");\n}\n",
    "pay/app.py": "",
}


def test_ai_fills_gaps_only_with_a_verified_quote(tmp_path, clean):
    repo = make(tmp_path / "shop", REGISTRY_REPO)

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        gaps = await s.call_tool("find_gaps", {"graph": GRAPH})
        bad = [await s.call_tool("add_connection", {"graph": GRAPH, **args}) for args in [
            {"caller": "orders", "callee": "pay", "file": "orders/Client.java", "line": 3, "quote": "paymentClient.charge()"},
            {"caller": "orders", "callee": "pay", "file": "orders/Client.java", "line": 40, "quote": "lb.call(Service.PAY, \"/charge\")"},
            {"caller": "orders", "callee": "pay", "file": "../../../etc/hosts", "line": 1, "quote": "localhost entry here"},
            {"caller": "orders", "callee": "gateway", "file": "orders/Client.java", "line": 3, "quote": "lb.call(Service.PAY, \"/charge\")"},
            {"caller": "orders", "callee": "payments", "file": "orders/Client.java", "line": 3, "quote": "lb.call(Service.PAY, \"/charge\")"},
        ]]
        good = await s.call_tool("add_connection", {"graph": GRAPH, "caller": "orders", "callee": "pay",
                                                     "file": "orders/Client.java", "line": 2,  # off by one is fine
                                                     "quote": "Response r = lb.call(Service.PAY, \"/charge\");",
                                                     "reason": "registry call"})
        again = await s.call_tool("add_connection", {"graph": GRAPH, "caller": "orders", "callee": "pay",
                                                      "file": "orders/Client.java", "line": 3,
                                                      "quote": "lb.call(Service.PAY, \"/charge\")"})
        after = await s.call_tool("service_calls", {"graph": GRAPH, "service": "orders"})
        return gaps, bad, good, again, after

    gaps, bad, good, again, after = session(steps)
    assert {g["service"] for g in gaps.structured_content["isolated"]} == {"gateway", "orders", "pay"}
    assert gaps.structured_content["isolated"][0]["folder"].endswith("gateway")
    reasons = [b.content[0].text for b in bad]
    assert all(b.is_error for b in bad)
    assert "not in orders/Client.java near line 3" in reasons[0]   # invented code
    assert "near line 40" in reasons[1]                            # right code, wrong place
    assert "outside the mapped repo" in reasons[2]                 # path traversal
    assert "does not mention 'gateway'" in reasons[3]              # real line, wrong callee
    assert "did you mean ['pay']" not in reasons[4] and "no service 'payments'" in reasons[4]
    g = good.structured_content
    assert (g["sources"], g["confidence"], g["already_known"]) == (["ai"], "inferred", False)
    assert g["evidence"] == ['ai | orders/Client.java:3 | Response r = lb.call(Service.PAY, "/charge");']  # real line kept
    assert again.structured_content["evidence"] == g["evidence"]  # idempotent
    (call,) = after.structured_content["calls"]
    assert (call["service"], call["confidence"]) == ("pay", "inferred")


def test_inferred_call_survives_remap_until_its_line_is_deleted(tmp_path, clean):
    repo = make(tmp_path / "shop", REGISTRY_REPO)
    add = {"graph": GRAPH, "caller": "orders", "callee": "pay", "file": "orders/Client.java", "line": 3,
           "quote": "lb.call(Service.PAY, \"/charge\")"}

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        await s.call_tool("add_connection", add)
        (repo / "orders/Client.java").write_text("class C {\n\n\n  // moved down\n  lb.call(Service.PAY, \"/charge\");\n}\n")
        moved = await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        (repo / "orders/Client.java").write_text("class C {}\n")
        deleted = await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        calls = await s.call_tool("service_calls", {"graph": GRAPH, "service": "orders"})
        return moved, deleted, calls

    moved, deleted, calls = session(steps)
    assert moved.structured_content["inferred_kept"] == 1 and moved.structured_content["stale_calls"] == 0
    assert deleted.structured_content["inferred_kept"] == 0 and deleted.structured_content["stale_calls"] == 1
    assert calls.structured_content["calls"][0]["stale"] is True  # kept for history, marked stale


def test_export_map_writes_the_graph_as_stored(tmp_path, clean):
    import json
    repo = make(tmp_path / "shop", REGISTRY_REPO)
    (tmp_path / "answers").mkdir()
    out = tmp_path / "answers" / "shop.json"

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        await s.call_tool("add_connection", {"graph": GRAPH, "caller": "orders", "callee": "pay", "file": "orders/Client.java",
                                             "line": 3, "quote": "lb.call(Service.PAY, \"/charge\")"})
        bad = [await s.call_tool("export_map", {"graph": GRAPH, "path": p}) for p in
               ("answers/shop.json", str(tmp_path / "answers" / "shop.txt"), str(tmp_path / "nope" / "x.json"))]
        ok = await s.call_tool("export_map", {"graph": GRAPH, "path": str(out)})
        return bad, ok

    bad, ok = session(steps)
    assert all(b.is_error for b in bad)
    assert ok.structured_content == {"path": str(out), "services": 3, "calls": 1, "agent_added": 1}
    data = json.loads(out.read_text())
    assert data["calls"] == [{"caller": "orders", "callee": "pay", "sources": ["ai"], "confidence": "inferred",
                              "evidence": ['ai | orders/Client.java:3 | lb.call(Service.PAY, "/charge")']}]
    assert {s["name"] for s in data["services"]} == {"gateway", "orders", "pay"} and "shop" in data["repos"]


# One app the scanner can't see (no compose/k8s entry), a duplicate name, and a folder-named service.
MESSY_REPO = {
    "docker-compose.yml": ("services:\n  front-end: {build: ./web}\n  cart: {build: ./cart}\n"
                           "  order-service-main: {build: ./orders/order-service-main}\n"),
    "k8s/frontend.yaml": ("apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: frontend}\nspec:\n  template:\n"
                          "    spec:\n      containers:\n      - name: app\n        image: web:1\n        env:\n"
                          "        - name: CART_ADDR\n          value: \"cart:80\"\n"),
    "web/package.json": '{"name": "frontend"}\n',
    "web/app.js": 'fetch("http://cart:80/items")\n',
    "cart/main.go": "",
    "cart/go.mod": "module cart\n",
    "cart/NOTES.txt": "cart-v2 is the rewrite of cart\n",
    "orders/order-service-main/build.gradle": "",
    "orders/order-service-main/src/main/resources/application.yml": "spring:\n  application:\n    name: order-service\n",
    "admin-ui/package.json": '{"name": "admin-ui"}\n',     # missed by the scanner: no deploy entry
    "admin-ui/src/api.js": 'export const CART = "http://cart:80";\n',
    "libs/common/pom.xml": "<project/>",                     # a shared library: deployable-looking, not a service
    "components/pubsub.yaml": "apiVersion: dapr.io/v1alpha1\nkind: Component\nmetadata:\n  name: orders-pubsub\n",
}


def test_find_gaps_lists_unmapped_folders_and_duplicates(tmp_path, clean):
    repo = make(tmp_path / "shop", MESSY_REPO)

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        return await s.call_tool("find_gaps", {"graph": GRAPH})

    g = session(steps).structured_content
    assert sorted(Path(f["folder"]).name for f in g["unmapped_folders"]) == ["admin-ui", "common"]
    assert {tuple(sorted(d["names"])) for d in g["possible_duplicates"]} == {("front-end", "frontend")}


def test_add_service_needs_proof_and_a_real_code_folder(tmp_path, clean):
    repo = make(tmp_path / "shop", MESSY_REPO)
    ok_args = {"graph": GRAPH, "name": "admin-ui", "kind": "service", "file": "admin-ui/package.json", "line": 1,
               "quote": '"name": "admin-ui"', "folder": "admin-ui", "reason": "Vue admin app"}

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        bad = [await s.call_tool("add_service", {**ok_args, **change}) for change in [
            {"name": "Admin UI"},                                      # not a valid name
            {"name": "cart"},                                          # already mapped
            {"kind": "database"},                                      # unknown kind
            {"quote": '"name": "admin-ui", "v": 2'},                   # not in the file
            {"name": "admin", "quote": '"name": "admin-ui"'},          # name only as part of a longer word
            {"folder": ""},                                            # a service needs code
            {"folder": "../.."},                                       # outside the repo
            {"folder": "admin-ui/src"},                                # no build file
            {"folder": "cart", "name": "cart-v2", "quote": "cart-v2 is the rewrite of cart", "file": "cart/NOTES.txt"},
        ]]
        good = await s.call_tool("add_service", ok_args)
        bus = await s.call_tool("add_service", {"graph": GRAPH, "name": "orders-pubsub", "kind": "infra",
                                                "file": "components/pubsub.yaml", "line": 4, "quote": "name: orders-pubsub"})
        link = await s.call_tool("add_connection", {"graph": GRAPH, "caller": "admin-ui", "callee": "cart",
                                                     "file": "admin-ui/src/api.js", "line": 1,
                                                     "quote": 'export const CART = "http://cart:80";'})
        remap = await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        (repo / "admin-ui/package.json").write_text('{"name": "renamed"}\n')
        gone = await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        return bad, good, bus, link, remap, gone

    bad, good, bus, link, remap, gone = session(steps)
    msgs = [b.content[0].text for b in bad]
    assert all(b.is_error for b in bad), msgs
    for msg, expect in zip(msgs, ["name must be", "already in the map", "kind must be", "not in admin-ui/package.json",
                                  "as a whole word", "needs its code folder", "outside the repo", "no build file",
                                  "already the code of 'cart'"]):
        assert expect in msg, (expect, msg)
    assert good.structured_content["source_dir"] == "admin-ui" and good.structured_content["origin"] == "ai"
    assert not bus.is_error and bus.structured_content["source_dir"] == ""
    assert not link.is_error
    assert remap.structured_content["inferred_kept"] == 3 and remap.structured_content["stale_services"] == 0
    assert gone.structured_content["stale_services"] == 1   # admin-ui's proof line changed: kept, marked stale


def test_merge_renames_to_the_real_name_and_survives_remap(tmp_path, clean):
    repo = make(tmp_path / "shop", MESSY_REPO)
    proof = {"file": "orders/order-service-main/src/main/resources/application.yml", "line": 3, "quote": "name: order-service"}

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        wrong = await s.call_tool("merge_services", {"graph": GRAPH, "keep": "order-service", "drop": "cart", **proof})
        done = await s.call_tool("merge_services", {"graph": GRAPH, "keep": "order-service", "drop": "order-service-main",
                                                    **proof, "reason": "spring.application.name"})
        remap = await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        names = await s.call_tool("list_maps", {})
        old = await s.call_tool("service_calls", {"graph": GRAPH, "service": "order-service-main"})
        new = await s.call_tool("service_calls", {"graph": GRAPH, "service": "order-service"})
        return wrong, done, remap, names, old, new

    wrong, done, remap, names, old, new = session(steps)
    assert wrong.is_error and "no proof these are the same service" in wrong.content[0].text  # quote is not in cart's code
    assert done.structured_content["action"] == "renamed"
    assert remap.structured_content["services"] == 4                     # not split again on re-map
    assert old.is_error and not new.is_error
    assert new.structured_content["source_dir"] == "orders/order-service-main"


def test_merge_folds_a_duplicate_and_keeps_its_calls(tmp_path, clean):
    repo = make(tmp_path / "shop", MESSY_REPO)

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        no_proof = await s.call_tool("merge_services", {"graph": GRAPH, "keep": "frontend", "drop": "cart",
                                                        "file": "k8s/frontend.yaml", "line": 3, "quote": "metadata: {name: frontend}"})
        done = await s.call_tool("merge_services", {"graph": GRAPH, "keep": "frontend", "drop": "front-end",
                                                    "file": "web/package.json", "line": 1, "quote": '"name": "frontend"'})
        remap = await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        fe = await s.call_tool("service_calls", {"graph": GRAPH, "service": "frontend"})
        return no_proof, done, remap, fe

    no_proof, done, remap, fe = session(steps)
    assert no_proof.is_error and "same service" in no_proof.content[0].text  # different folders, quote doesn't name cart
    assert done.structured_content["action"] == "merged" and done.structured_content["moved"]["calls_out"] == 1
    assert remap.structured_content["services"] == 3
    calls = {c["service"]: c for c in fe.structured_content["calls"]}
    assert set(calls) == {"cart"} and len(calls["cart"]["evidence"]) == 2  # k8s CART_ADDR and web/app.js, merged


def k8s(name, image, extra=""):
    return ("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: %s\nspec:\n  template:\n    metadata:\n"
            "      annotations: {%s}\n    spec:\n      containers:\n      - name: app\n        image: %s\n") % (name, extra, image)


# Dapr-style: services exist only as deploy names; code lives in differently named folders (.NET, Java, monorepo).
FOLDERLESS_REPO = {
    "deploy/order.yaml": k8s("order-service", "ghcr.io/acme/shop-order-service:1", 'dapr.io/app-id: "order-service"'),
    "deploy/make-line.yaml": k8s("make-line-service", "ghcr.io/acme/shop-make-line:1"),
    "deploy/billing.yaml": k8s("billing", "ghcr.io/acme/billing:1"),
    "deploy/ui.yaml": k8s("ui", "ghcr.io/acme/ui:1"),
    "Acme.OrderService/Acme.OrderService.csproj": "<Project/>",
    "Acme.OrderService/Program.cs": 'const string AppId = "order-service";\n',
    "services/make-line/src/package.json": '{"name": "make-line"}',
    "Acme.UI/package.json": '{"name": "acme-ui"}',
    "Acme.UI/vue.config.js": "// ui\nmodule.exports = { pages: 'ui' }\n",
    "billing-v1/pom.xml": "<project/>", "billing-v2/pom.xml": "<project/>",   # two folders fit "billing"
    "ci/build-ui.yml": "      - run: docker build -t ui Acme.UI\n",
}


def test_attach_folders_by_convention_or_cited_line(tmp_path, clean):
    repo = make(tmp_path / "shop", FOLDERLESS_REPO)

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        gaps = await s.call_tool("find_gaps", {"graph": GRAPH})
        conv = await s.call_tool("set_service_folder", {"graph": GRAPH, "service": "order-service", "folder": "Acme.OrderService"})
        mono = await s.call_tool("set_service_folder", {"graph": GRAPH, "service": "make-line-service", "folder": "services/make-line/src"})
        ambiguous = await s.call_tool("set_service_folder", {"graph": GRAPH, "service": "billing", "folder": "billing-v1"})
        short = await s.call_tool("set_service_folder", {"graph": GRAPH, "service": "ui", "folder": "Acme.UI"})
        wrong_line = await s.call_tool("set_service_folder", {"graph": GRAPH, "service": "ui", "folder": "Acme.UI",
                                                              "file": "deploy/ui.yaml", "line": 4, "quote": "name: ui"})
        cited = await s.call_tool("set_service_folder", {"graph": GRAPH, "service": "ui", "folder": "Acme.UI",
                                                         "file": "ci/build-ui.yml", "line": 1, "quote": "docker build -t ui Acme.UI"})
        taken = await s.call_tool("set_service_folder", {"graph": GRAPH, "service": "billing", "folder": "Acme.OrderService"})
        remap = await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        order = await s.call_tool("service_calls", {"graph": GRAPH, "service": "order-service"})
        return gaps, conv, mono, ambiguous, short, wrong_line, cited, taken, remap, order

    gaps, conv, mono, ambiguous, short, wrong_line, cited, taken, remap, order = session(steps)
    hints = {Path(u["folder"]).name: u.get("may_belong_to") for u in gaps.structured_content["unmapped_folders"]}
    assert hints["Acme.OrderService"] == ["order-service"] and hints["Acme.UI"] is None  # "ui" too short to guess
    assert conv.structured_content["proof"].startswith("convention") and mono.structured_content["folder"] == "services/make-line/src"
    assert ambiguous.is_error and "other free folders also fit" in ambiguous.content[0].text
    assert short.is_error and "doesn't match" in short.content[0].text
    assert wrong_line.is_error and "inside the folder, or name the folder" in wrong_line.content[0].text
    assert cited.structured_content["proof"].startswith("ai | ci/build-ui.yml:1")
    assert taken.is_error and "overlaps the code of 'order-service'" in taken.content[0].text
    assert order.structured_content["source_dir"] == "Acme.OrderService"     # kept across re-map
    assert order.structured_content["kind"] == "service"                    # now scanned as code


def test_merge_by_shared_image_but_never_by_public_image(tmp_path, clean):
    repo = make(tmp_path / "shop", {
        "docker-compose.yml": ("services:\n  user: {image: 'gcr.io/acme/shop-user:2.0'}\n"
                               "  user-db: {image: 'mongo:6'}\n  cart-db: {image: 'mongo:6'}\n"),
        "k8s/users.yaml": k8s("users", "gcr.io/acme/shop-user:latest"),
        "k8s/front.yaml": k8s("front", "gcr.io/acme/front:1", 'note: "USERS_HOST=users"'),
    })

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        public = await s.call_tool("merge_services", {"graph": GRAPH, "keep": "user-db", "drop": "cart-db",
                                                      "file": "docker-compose.yml", "line": 3, "quote": "user-db: {image: 'mongo:6'}"})
        same = await s.call_tool("merge_services", {"graph": GRAPH, "keep": "users", "drop": "user",
                                                    "file": "k8s/front.yaml", "line": 6, "quote": 'USERS_HOST=users'})
        return public, same

    public, same = session(steps)
    assert public.is_error and "same image built for this system" in public.content[0].text
    assert not same.is_error and same.structured_content["action"] == "merged"


def test_undo_only_touches_what_an_agent_added(tmp_path, clean):
    repo = make(tmp_path / "shop", MESSY_REPO)

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        await s.call_tool("add_service", {"graph": GRAPH, "name": "admin-ui", "kind": "service", "file": "admin-ui/package.json",
                                          "line": 1, "quote": '"name": "admin-ui"', "folder": "admin-ui"})
        await s.call_tool("add_connection", {"graph": GRAPH, "caller": "admin-ui", "callee": "cart",
                                             "file": "admin-ui/src/api.js", "line": 1, "quote": 'export const CART = "http://cart:80";'})
        no = [await s.call_tool("remove_service", {"graph": GRAPH, "name": "cart", "reason": "x"}),
              await s.call_tool("remove_connection", {"graph": GRAPH, "caller": "front-end", "callee": "cart", "reason": "x"}),
              await s.call_tool("remove_connection", {"graph": GRAPH, "caller": "cart", "callee": "admin-ui", "reason": "x"})]
        rc = await s.call_tool("remove_connection", {"graph": GRAPH, "caller": "admin-ui", "callee": "cart", "reason": "typo"})
        await s.call_tool("add_connection", {"graph": GRAPH, "caller": "admin-ui", "callee": "cart",
                                             "file": "admin-ui/src/api.js", "line": 1, "quote": 'export const CART = "http://cart:80";'})
        rs = await s.call_tool("remove_service", {"graph": GRAPH, "name": "admin-ui", "reason": "test node"})
        gone = await s.call_tool("service_calls", {"graph": GRAPH, "service": "admin-ui"})
        return no, rc, rs, gone

    no, rc, rs, gone = session(steps)
    assert all(n.is_error for n in no)
    assert "found by the parser" in no[0].content[0].text and "found by the parser" in no[1].content[0].text
    assert "no call cart -> admin-ui" in no[2].content[0].text
    assert not rc.is_error and rs.structured_content["calls_removed"] == 1 and gone.is_error


def test_find_gaps_on_a_config_only_repo(tmp_path, clean):
    repo = make(tmp_path / "cfg", {"k8s/a.yaml": k8s("a", "x/a:1"), "k8s/b.yaml": k8s("b", "x/b:1")})

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        return await s.call_tool("find_gaps", {"graph": GRAPH})

    g = session(steps)
    assert not g.is_error and g.structured_content["isolated"] == [] and g.structured_content["services"] == 0


def test_read_only_query_params_algorithms_and_nodes(tmp_path, clean):
    repo = make(tmp_path / "shop", {**MESSY_REPO})
    q = lambda query, **kw: {"graph": GRAPH, "query": query, **kw}

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        rows = await s.call_tool("query_graph_readonly", q("MATCH (a:Service)-[c:CALLS]->(b:Service) WHERE b.name = $n "
                                                           "RETURN a.name, c.confidence ORDER BY a.name", params={"n": "cart"}))
        node = await s.call_tool("query_graph_readonly", q("MATCH (s:Service {name: 'cart'}) RETURN s"))
        path = await s.call_tool("query_graph_readonly", q("MATCH p = (a:Service {name:'front-end'})-[:CALLS*1..2]->(b) RETURN p LIMIT 1"))
        algo = await s.call_tool("query_graph_readonly", q("CALL algo.pageRank('Service','CALLS') YIELD node, score "
                                                           "RETURN node.name, score ORDER BY score DESC LIMIT 1"))
        capped = await s.call_tool("query_graph_readonly", q("UNWIND range(1, 50) AS i RETURN i", max_rows=10))
        bad = [await s.call_tool("query_graph_readonly", q(x)) for x in (
            "CREATE (:Hack {x: 1})", "MATCH (n) DETACH DELETE n", "MATCH (s:Service) SET s.owner = 'x' RETURN s",
            "MATCH (n RETURN n", "CALL db.idx.fulltext.createNodeIndex('Service', 'name')")]
        still = await s.call_tool("query_graph_readonly", q("MATCH (s:Service) RETURN count(s)"))
        return rows, node, path, algo, capped, bad, still

    rows, node, path, algo, capped, bad, still = session(steps)
    assert rows.structured_content["columns"] == ["a.name", "c.confidence"] and rows.structured_content["row_count"] >= 1
    n = node.structured_content["rows"][0][0]
    assert n["labels"] == ["Service"] and n["properties"]["name"] == "cart"            # nodes come back as plain data
    assert path.structured_content["rows"][0][0]["path"][0]["properties"]["name"] == "front-end"
    assert len(algo.structured_content["rows"][0]) == 2 and algo.structured_content["rows"][0][1] > 0
    assert capped.structured_content["row_count"] == 50 and len(capped.structured_content["rows"]) == 10
    assert capped.structured_content["truncated"] is True
    assert all(b.is_error and "FalkorDB rejected the query" in b.content[0].text for b in bad), [b.content[0].text for b in bad]
    assert still.structured_content["rows"] == [[4]]                                    # nothing was written or deleted


def test_query_timeout_is_enforced(tmp_path, clean):
    g = FalkorDB().select_graph(GRAPH)
    g.query("UNWIND range(1, 500) AS i CREATE (:N {i: i})")

    async def steps(s):
        slow = await s.call_tool("query_graph_readonly", {"graph": GRAPH, "timeout_ms": 100,
                                                          "query": "MATCH (a:N), (b:N), (c:N) RETURN count(*)"})
        fast = await s.call_tool("query_graph_readonly", {"graph": GRAPH, "query": "MATCH (n:N) RETURN count(n)"})
        return slow, fast

    slow, fast = session(steps)
    assert slow.is_error and "timed out" in slow.content[0].text.lower()
    assert fast.structured_content["rows"] == [[500]]


def test_schema_and_graph_list(tmp_path, clean):
    repo = make(tmp_path / "shop", {**MESSY_REPO})

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        schema = await s.call_tool("get_graph_schema", {"graph": GRAPH})
        missing = await s.call_tool("get_graph_schema", {"graph": "no_such_graph_xyz"})
        graphs = await s.call_tool("list_graphs", {})
        bad = await s.call_tool("get_graph_schema", {"graph": "bad name!"})
        return schema, missing, graphs, bad

    schema, missing, graphs, bad = session(steps)
    sc = schema.structured_content
    labels = {l["label"]: l for l in sc["labels"]}
    assert labels["Service"]["count"] == 4 and labels["Service"]["properties"]["name"] == 4
    assert any(c["from"] == "Service" and c["type"] == "CALLS" and c["to"] == "Service" for c in sc["connections"])
    assert {r["type"] for r in sc["relationship_types"]} >= {"CALLS"}
    assert missing.is_error and "empty or does not exist" in missing.content[0].text
    assert bad.is_error and "graph name must be" in bad.content[0].text
    listed = {g["graph"]: g["nexttrace_map"] for g in graphs.structured_content["graphs"]}
    assert listed[GRAPH] is True


def test_connection_settings_reach_the_client(monkeypatch):
    from nexttrace import graphdb
    seen = {}
    import falkordb
    monkeypatch.setattr(falkordb, "FalkorDB", lambda **kw: seen.update(kw))
    for k in ("FALKORDB_HOST", "FALKORDB_PORT", "FALKORDB_USERNAME", "FALKORDB_PASSWORD", "FALKORDB_TLS"):
        monkeypatch.delenv(k, raising=False)
    graphdb.connect()
    assert seen == {"host": "localhost", "port": 6379, "username": None, "password": None, "ssl": False}
    monkeypatch.setenv("FALKORDB_HOST", "db.example.cloud")
    monkeypatch.setenv("FALKORDB_PORT", "6380")
    monkeypatch.setenv("FALKORDB_USERNAME", "agent")
    monkeypatch.setenv("FALKORDB_PASSWORD", "s3cret")
    monkeypatch.setenv("FALKORDB_TLS", "true")
    graphdb.connect()
    assert seen == {"host": "db.example.cloud", "port": 6380, "username": "agent", "password": "s3cret", "ssl": True}
    monkeypatch.setenv("FALKORDB_USERNAME", "")
    monkeypatch.setenv("FALKORDB_PASSWORD", "")
    monkeypatch.setenv("FALKORDB_TLS", "false")
    graphdb.connect()
    assert seen["username"] is None and seen["password"] is None and seen["ssl"] is False   # empty means unset


def test_failure_tools_over_mcp(tmp_path, clean):
    repo = make(tmp_path / "shop", {**MESSY_REPO})

    async def steps(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        blast = await s.call_tool("blast_radius", {"graph": GRAPH, "service": "cart"})
        paths = await s.call_tool("failure_paths", {"graph": GRAPH, "service": "cart"})
        crit = await s.call_tool("criticality", {"graph": GRAPH, "top": 3})
        errs = [await s.call_tool("blast_radius", {"graph": GRAPH, "service": "carts"}),
                await s.call_tool("blast_radius", {"graph": GRAPH, "service": "cart", "max_depth": 50}),
                await s.call_tool("criticality", {"graph": GRAPH, "kind": "db"}),
                await s.call_tool("failure_paths", {"graph": "bad name!", "service": "cart"})]
        return blast, paths, crit, errs

    blast, paths, crit, errs = session(steps)
    b = blast.structured_content
    assert {a["service"] for a in b["affected"]} == {"front-end", "frontend"}
    assert all(h["evidence"] for a in b["affected"] for h in a["hops"]) and b["cited_connections"]
    p = paths.structured_content
    assert {tuple(x["path"]) for x in p["paths"]} == {("front-end", "cart"), ("frontend", "cart")}
    c = crit.structured_content
    assert c["ranked"][0]["service"] == "cart" and c["ranked"][0]["dependents"] == 2
    assert [e.is_error for e in errs] == [True] * 4
    msgs = [e.content[0].text for e in errs]
    assert "did you mean ['cart']" in msgs[0] and "max_depth" in msgs[1] and "kind must be" in msgs[2] and "graph name" in msgs[3]


def test_three_agents_in_separate_processes_hand_off_through_the_graph(tmp_path, clean):
    """Triage, investigator and scribe each run in their own MCP server process: the graph is all they share."""
    repo = make(tmp_path / "shop", {**MESSY_REPO})
    alarm = [{"service": "cart", "signal": "latency-p90", "at": 1000}, {"service": "front-end", "signal": "errors", "at": 1000}]

    async def triage(s):
        await s.call_tool("map_repo", {"path": str(repo), "graph": GRAPH})
        sid = (await s.call_tool("start_session", {"graph": GRAPH, "agent": "triage", "task": "new alarms"})).structured_content["session"]
        out = await s.call_tool("triage_alarms", {"graph": GRAPH, "alarms": alarm, "agent": "triage", "session": sid})
        return out.structured_content["incidents"][0]["incident"]
    inc = session(triage)

    async def investigator(s):
        sid = (await s.call_tool("start_session", {"graph": GRAPH, "agent": "investigator"})).structured_content["session"]
        todo = (await s.call_tool("list_incidents", {"graph": GRAPH, "status": "open"})).structured_content["incidents"]
        v = (await s.call_tool("get_incident", {"graph": GRAPH, "incident": todo[0]["incident"]})).structured_content["version"]
        await s.call_tool("claim_incident", {"graph": GRAPH, "incident": inc, "agent": "investigator", "from_status": "open",
                                             "to_status": "investigating", "version": v, "session": sid})
        b = (await s.call_tool("blast_radius", {"graph": GRAPH, "service": "cart"})).structured_content
        made_up = await s.call_tool("record_finding", {"graph": GRAPH, "incident": inc, "agent": "investigator", "cause": "cart",
                                                       "reason": "x", "cited": [{"caller": "admin-ui", "callee": "cart"}]})
        cited = [{"caller": c["caller"], "callee": c["callee"]} for c in b["cited_connections"]]
        check = (await s.call_tool("check_answer", {"graph": GRAPH, "connections": cited + [{"caller": "admin-ui", "callee": "cart"}],
                                                    "services": ["front-end"], "target": "cart"})).structured_content
        assert not check["ok"] and check["not_on_map"] == [{"caller": "admin-ui", "callee": "cart"}]
        assert len(check["verified"]) == len(cited) and check["unsupported_services"] == []
        ok = await s.call_tool("record_finding", {"graph": GRAPH, "incident": inc, "agent": "investigator", "cause": "cart",
                                                  "reason": "front-end errors follow cart latency", "cited": cited,
                                                  "session": sid})
        return made_up, ok
    made_up, ok = session(investigator)
    assert made_up.is_error and "not on the map" in made_up.content[0].text
    assert ok.structured_content["evidence"] and all("|" in e for e in ok.structured_content["evidence"])

    async def scribe(s):
        sid = (await s.call_tool("start_session", {"graph": GRAPH, "agent": "scribe"})).structured_content["session"]
        early = await s.call_tool("claim_incident", {"graph": GRAPH, "incident": inc, "agent": "scribe",
                                                     "from_status": "open", "to_status": "investigating"})
        done = await s.call_tool("resolve_incident", {"graph": GRAPH, "incident": inc, "agent": "scribe",
                                                      "fix": "raise cart pool size", "runbook": "cart pool", "session": sid})
        recall = await s.call_tool("similar_incidents", {"graph": GRAPH, "services": ["cart"]})
        tl = await s.call_tool("incident_timeline", {"graph": GRAPH, "incident": inc})
        hist = await s.call_tool("agent_history", {"graph": GRAPH, "incident": inc})
        return early, done, recall, tl, hist
    early, done, recall, tl, hist = session(scribe)
    assert early.is_error and "conflict" in early.content[0].text
    assert done.structured_content["status"] == "resolved"
    assert recall.structured_content["similar"][0]["fix"] == "raise cart pool size"
    assert [e["agent"] for e in tl.structured_content["events"]] == ["triage", "investigator", "investigator", "scribe"]
    assert {h["tool"] for h in hist.structured_content["steps"]} >= {"claim_incident", "record_finding", "resolve_incident"}
