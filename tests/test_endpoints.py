"""Endpoint-level map: routes per framework, call sites, linking rules, graph writes, real repos."""
from pathlib import Path

import pytest
from falkordb import FalkorDB

from nexttrace import endpoints as ep
from nexttrace import mapper

REPOS = Path(__file__).parent.parent / "data" / "repos"
COMPOSE = "services:\n" + "".join(f"  {s}: {{build: ./{s}}}\n" for s in ("web", "orders", "seats", "pay", "shop"))


def make(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def run(repo):
    m = mapper.scan(repo)
    routes, owner = ep.routes(repo, m)
    return m, routes, ep.link(ep.calls(repo, m, owner), routes)


def linked(pairs):
    return {(c.caller, r.key if r else None) for c, r in pairs}


SEATS_CONTROLLER = """
@RestController
@RequestMapping("/api/v1/seats")
public class SeatController {
    @GetMapping(path = "/list")
    public List all() {}
    @GetMapping(path = "/{seatId}")
    public Seat one(@PathVariable String seatId) {}
    @RequestMapping(value = "/{seatId}", method = RequestMethod.DELETE)
    public void drop() {}
    @PostMapping
    public Seat create() {}
}
"""


def test_spring_routes_and_java_calls(tmp_path):
    repo = make(tmp_path / "r", {
        "docker-compose.yml": COMPOSE,
        "seats/src/main/java/SeatController.java": SEATS_CONTROLLER,
        "orders/src/main/java/Orders.java": """
class Orders {
    String base = "http://seats:8080/api/v1/seats";
    void a() {
        restTemplate.exchange("http://seats:8080/api/v1/seats/" + seatId,
            HttpMethod.DELETE, entity, Response.class);
    }
    void b() { restTemplate.getForObject(base + "/list", List.class); }
    void c() { log.info("see base docs at http://wiki/base"); }  // "base" inside a string is not a use
}
""",
    })
    _, routes, pairs = run(repo)
    assert {r.key for r in routes} == {"seats GET /api/v1/seats/list", "seats GET /api/v1/seats/{seatId}",
                                       "seats DELETE /api/v1/seats/{seatId}", "seats POST /api/v1/seats"}
    # "/seats/" + id with DELETE goes to the {seatId} route, not to POST /seats or GET /list
    assert linked(pairs) == {("orders", "seats DELETE /api/v1/seats/{seatId}"), ("orders", "seats GET /api/v1/seats/list")}


def test_url_variable_if_else_and_reuse(tmp_path):
    repo = make(tmp_path / "r", {
        "docker-compose.yml": COMPOSE,
        "seats/src/main/java/SeatController.java": SEATS_CONTROLLER,
        "pay/app.py": "@app.route('/charge/<cid>', methods=['POST'])\ndef charge(cid): ...\n",
        "orders/src/main/java/Orders.java": """
class Orders {
    void pick(boolean fast) {
        String url = "";
        if (fast) {
            url = "http://seats:8080/api/v1/seats/" + id;
        } else {
            url = "http://pay:80/charge/" + id;
        }
        restTemplate.exchange(url, HttpMethod.POST, e, R.class);
    }
    void later() {
        String url = "http://seats:8080/api/v1/seats/list";
        restTemplate.exchange(url, HttpMethod.GET, e, R.class);
    }
}
""",
    })
    _, _, pairs = run(repo)
    got = linked(pairs)
    assert ("orders", "pay POST /charge/<cid>") in got                 # both if/else branches are kept
    assert ("orders", "seats GET /api/v1/seats/list") in got
    # the POST to ".../seats/" + id matches no route: kept as unmatched, a likely bug
    assert ("orders", None) in got
    assert not any(c.path.startswith("/charge") and c.method == "GET" for c, _ in pairs)  # no cross-method mixing


def test_route_styles_express_flask_fastapi_go(tmp_path):
    repo = make(tmp_path / "r", {
        "docker-compose.yml": COMPOSE,
        "web/server.js": "app.get('/cart', h)\nrouter.post(\"/cart/:id\", h)\n// app.get('/old', h)\n",
        "pay/app.py": "@app.route('/pay')\ndef p(): ...\n@router.put('/refund/{rid}')\ndef r(): ...\n",
        "shop/main.go": ('r.Methods("GET").Path("/items/{id}").Handler(h)\n'
                         'http.HandleFunc("/health", h)\n'),
    })
    _, routes, _ = run(repo)
    assert {r.key for r in routes} == {"web GET /cart", "web POST /cart/:id", "pay GET /pay", "pay PUT /refund/{rid}",
                                       "shop GET /items/{id}", "shop ANY /health"}


def test_grpc_proto_go_and_python_clients(tmp_path):
    repo = make(tmp_path / "r", {
        "docker-compose.yml": "services:\n  cartservice: {build: ./cart}\n  checkout: {build: ./checkout}\n  recs: {build: ./recs}\n",
        "protos/demo.proto": "service CartService {\n  rpc GetCart(Req) returns (Cart) {}\n  rpc EmptyCart(Req) returns (E) {}\n}\n",
        "cart/server.cs": "",
        "checkout/main.go": ("cart, err := pb.NewCartServiceClient(cs.cartConn).GetCart(ctx, req)\n"
                             "_, err = pb.NewCartServiceClient(cs.cartConn).\n\t\tEmptyCart(ctx, req)\n"),
        "recs/client.py": "stub = demo_pb2_grpc.CartServiceStub(channel)\nresp = stub.GetCart(req)\n",
    })
    _, routes, pairs = run(repo)
    assert {r.key for r in routes} == {"cartservice RPC CartService/GetCart", "cartservice RPC CartService/EmptyCart"}
    assert linked(pairs) == {("checkout", "cartservice RPC CartService/GetCart"),
                             ("checkout", "cartservice RPC CartService/EmptyCart"),
                             ("recs", "cartservice RPC CartService/GetCart")}


@pytest.fixture
def graph():
    db = FalkorDB()
    for name in ("test_endpoints",):
        if name in db.list_graphs():
            db.select_graph(name).delete()
    yield "test_endpoints"
    db.select_graph("test_endpoints").delete()


def test_written_to_graph_idempotent_stale_and_cross_repo(tmp_path, graph):
    seats = make(tmp_path / "seats", {"Dockerfile": "FROM java", "src/main/java/SeatController.java": SEATS_CONTROLLER})
    orders = make(tmp_path / "orders", {"Dockerfile": "FROM java", "src/main/java/O.java":
                  'void a() { rest.exchange("http://seats/api/v1/seats/" + id, HttpMethod.GET, e, R.class); }\n'})
    s1 = mapper.map_repo(seats, graph)
    o1 = mapper.map_repo(orders, graph)                       # route lives in another repo: still linked
    o2 = mapper.map_repo(orders, graph)
    assert s1["endpoints"] == 4 and o1["endpoint_calls"] == o1["endpoint_calls_matched"] == 1
    assert (o2["endpoint_calls"], o2["stale_endpoint_calls"]) == (1, 0)
    g = FalkorDB().select_graph(graph)
    q = "MATCH (a:Service)-[c:CALLS_ENDPOINT]->(e:Endpoint) RETURN a.name, e.key, c.matched, c.stale"
    assert g.ro_query(q).result_set == [["orders", "seats GET /api/v1/seats/{seatId}", True, False]]

    (orders / "src/main/java/O.java").write_text("class O {}\n")  # the call is removed
    assert mapper.map_repo(orders, graph)["stale_endpoint_calls"] == 1
    assert g.ro_query(q).result_set == [["orders", "seats GET /api/v1/seats/{seatId}", True, True]]


@pytest.mark.skipif(not (REPOS / "boutique").exists(), reason="clone microservices-demo into data/repos/boutique")
def test_boutique_grpc_calls_all_link():
    _, routes, pairs = run((REPOS / "boutique").resolve())
    assert sum(r.method == "RPC" for r in routes) >= 15
    assert pairs and all(r for _, r in pairs)
    assert ("checkoutservice", "paymentservice RPC PaymentService/Charge") in linked(pairs)


@pytest.mark.skipif(not (REPOS / "train-ticket").exists(), reason="clone train-ticket v0.2.1 into data/repos/train-ticket")
def test_train_ticket_links_and_flags_the_post_delete_mismatch():
    _, routes, pairs = run((REPOS / "train-ticket").resolve())
    unmatched = {(c.caller, c.callee, c.method) for c, r in pairs if not r}
    assert len(routes) >= 250 and sum(1 for _, r in pairs if r) >= 160
    # deleteOrder() POSTs to /order/{id}, which only accepts DELETE: a real mismatch in the code
    assert unmatched == {("ts-rebook-service", "ts-order-service", "POST"),
                         ("ts-rebook-service", "ts-order-other-service", "POST")}
