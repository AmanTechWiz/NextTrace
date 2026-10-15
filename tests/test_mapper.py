"""Mapper rules on tiny fixture repos, plus checks against the real repos when they're cloned."""
from pathlib import Path

import pandas as pd
import pytest
from falkordb import FalkorDB

from nexttrace import mapper

REPOS = Path(__file__).parent.parent / "data" / "repos"


def make(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


K8S = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: %s}
spec:
  template:
    spec:
      containers:
      - name: app
        image: %s
        env:%s
"""


def deploy(name, image="app:1", env=()):
    items = "".join(f"\n        - name: {k}\n          value: \"{v}\"" for k, v in env)
    return K8S % (name, image, items or " []")


def test_k8s_env_address_becomes_edge_with_line(tmp_path):
    repo = make(tmp_path / "shop", {
        "k8s/web.yaml": deploy("web", env=[("CART_ADDR", "cart:7070"), ("NOTE", "the cart is great")]),
        "k8s/cart.yaml": deploy("cart"),
        "k8s/db.yaml": deploy("cart-db", image="mongo:6"),
        "cart/main.go": "// talks to cart-db:27017 (comment)\nconn := dial(\"cart-db:27017\")\n",
        "web/app.js": "",
    })
    m = mapper.scan(repo)
    assert set(m.edges) == {("web", "cart"), ("cart", "cart-db")}
    (ev,) = m.edges[("web", "cart")]
    assert ev.source == "config" and ev.where.startswith("k8s/web.yaml:") and ev.text == "cart:7070"
    (ev,) = m.edges[("cart", "cart-db")]
    assert ev.where == "cart/main.go:2"  # the comment on line 1 is skipped
    assert m.services["cart-db"]["kind"] == "datastore" and m.services["cart"]["kind"] == "service"


def test_compose_depends_on_environment_and_build_context(tmp_path):
    repo = make(tmp_path / "app", {
        "docker-compose.yml": (
            "services:\n"
            "  api:\n    build: ./api\n    depends_on: [db]\n    environment:\n      - PAY_URL=http://pay:8080/charge\n"
            "  pay:\n    build:\n      context: ./payments\n"
            "  db:\n    image: postgres:16\n    hostname: db\n"),
        "api/server.py": "x = 1\n",
        "payments/index.js": "",
    })
    m = mapper.scan(repo)
    assert m.services["api"]["source_dir"] == "api" and m.services["pay"]["source_dir"] == "payments"
    assert set(m.edges) == {("api", "db"), ("api", "pay")}
    assert m.services["db"]["kind"] == "datastore"


def test_code_patterns_and_noise(tmp_path):
    repo = make(tmp_path / "mono", {
        "docker-compose.yml": "services:\n  orders: {build: ./orders}\n  payment: {build: ./payment}\n  user: {build: ./user}\n",
        "orders/Config.java": (
            'return new ServiceUri(new Hostname("payment"), domain, "/paymentAuth");\n'
            'log.info("user");\n'                       # bare word: not a call
            'String u = "http://user/customers";\n'),
        "orders/test/Fake.java": 'String x = "http://payment:80";\n',  # tests are skipped
        "payment/app.py": "", "user/app.py": "",
    })
    m = mapper.scan(repo)
    assert set(m.edges) == {("orders", "payment"), ("orders", "user")}
    assert all(e.source == "code" for e in m.edges[("orders", "payment")])


def test_docs_folder_is_not_a_service(tmp_path):
    repo = make(tmp_path / "r", {"k8s/a.yaml": deploy("a"), "old-docs/k8s/legacy.yaml": deploy("legacy-service")})
    assert set(mapper.scan(repo).services) == {"a"}


def test_polyrepo_root_dockerfile_and_cross_repo_known(tmp_path):
    repo = make(tmp_path / "front-end", {"Dockerfile": "FROM node", "api/endpoints.js": 'u = "http://catalogue/items"\n'})
    m = mapper.scan(repo, known={"catalogue"})
    assert m.services["front-end"]["source_dir"] == "." and m.services["front-end"]["kind"] == "service"
    assert set(m.edges) == {("front-end", "catalogue")}
    assert set(mapper.scan(repo).edges) == set()  # unknown hosts are not invented


def test_backstage_owner_and_depends_on_then_codeowners(tmp_path):
    repo = make(tmp_path / "r", {
        "svc-a/catalog-info.yaml": ("apiVersion: backstage.io/v1alpha1\nkind: Component\n"
                                    "metadata: {name: svc-a}\nspec: {owner: group:default/team-a, dependsOn: ['resource:default/a-db']}\n"),
        "infra/catalog-info.yaml": "apiVersion: backstage.io/v1alpha1\nkind: Resource\nmetadata: {name: a-db}\nspec: {}\n",
        "k8s/b.yaml": deploy("svc-b"), "svc-b/main.go": "",
        ".github/CODEOWNERS": "*  @everyone\n/svc-b/ @team-b\n",
    })
    m = mapper.scan(repo)
    assert m.services["svc-a"]["owner"] == "team-a" and m.services["svc-b"]["owner"] == "team-b"
    assert m.services["a-db"]["kind"] == "datastore"
    assert ("svc-a", "a-db") in m.edges


def test_traces_add_high_confidence_and_report_unknown(tmp_path):
    repo = make(tmp_path / "r", {"k8s/a.yaml": deploy("a"), "k8s/b.yaml": deploy("b")})
    m = mapper.scan(repo)
    spans = pd.DataFrame({"spanID": ["1", "2", "3"], "parentSpanID": [None, "1", "1"], "serviceName": ["a", "b", "ghost"]})
    unknown = mapper.add_traces(m, spans, "t.csv")
    (row,) = mapper.edge_rows(m)
    assert (row["caller"], row["callee"], row["confidence"], row["sources"]) == ("a", "b", "high", ["trace"])
    assert unknown == {("a", "ghost"): 1}


@pytest.fixture
def graph():
    db = FalkorDB()
    g = db.select_graph("test_mapper")
    if "test_mapper" in db.list_graphs():
        g.delete()
    yield g
    g.delete()


def test_write_is_idempotent_and_marks_stale_per_repo(tmp_path, graph):
    one = make(tmp_path / "one", {"k8s/a.yaml": deploy("a", env=[("B", "b:1")]), "k8s/b.yaml": deploy("b")})
    two = make(tmp_path / "two", {"Dockerfile": "FROM x", "app.py": 'requests.get("http://a/x")\n'})
    first = mapper.write(graph, mapper.scan(one))
    again = mapper.write(graph, mapper.scan(one))
    assert (first["services"], first["calls"]) == (again["services"], again["calls"]) == (2, 1)
    assert graph.ro_query("MATCH (s:Service) RETURN count(s)").result_set == [[2]]

    cross = mapper.write(graph, mapper.scan(two, known=mapper.known_services(graph, "two")))
    assert cross["cross_repo_calls"] == 1

    (one / "k8s/a.yaml").write_text(deploy("a"))  # a no longer calls b
    after = mapper.write(graph, mapper.scan(one))
    assert after["stale_calls"] == 1 and after["stale_services"] == 0
    rows = graph.ro_query("MATCH (x)-[c:CALLS]->(y) RETURN x.name, y.name, c.stale ORDER BY x.name").result_set
    assert rows == [["a", "b", True], ["two", "a", False]]  # kept, marked stale; other repo untouched


@pytest.mark.skipif(not (REPOS / "boutique").exists(), reason="clone microservices-demo into data/repos/boutique")
def test_boutique_matches_googles_diagram():
    diagram = {("frontend", s) for s in ("adservice", "recommendationservice", "productcatalogservice", "cartservice",
                                          "shippingservice", "currencyservice", "checkoutservice")}
    diagram |= {("checkoutservice", s) for s in ("productcatalogservice", "cartservice", "shippingservice",
                                                 "currencyservice", "paymentservice", "emailservice")}
    diagram |= {("recommendationservice", "productcatalogservice"), ("cartservice", "redis-cart"), ("loadgenerator", "frontend")}
    assert diagram <= set(mapper.scan(REPOS / "boutique").edges)


@pytest.mark.skipif(not (REPOS / "train-ticket").exists(), reason="clone train-ticket v0.2.1 into data/repos/train-ticket")
def test_train_ticket_is_deterministic():
    assert mapper.edge_rows(mapper.scan(REPOS / "train-ticket")) == mapper.edge_rows(mapper.scan(REPOS / "train-ticket"))


def test_quoted_names_count_only_in_host_calls_annotations_and_env_defaults(tmp_path):
    repo = make(tmp_path / "r", {
        "docker-compose.yml": "services:\n" + "".join(f"  {s}: {{build: ./{s}}}\n" for s in ("a", "b", "c", "d", "e")),
        "a/Auth.java": 'clients.inMemory().withClient("b").secret("x");\nlog.info("calling c now");\n',  # not calls
        "a/Client.java": '@FeignClient(name = "c")\ninterface CClient {}\n',
        "a/app.py": 'D = os.getenv("D_HOST", "d")\n',
        "a/server.js": "const e = require('e');\nvar eHost = process.env.E_ENDPOINT || 'e';\n",
        "b/x.go": "", "c/x.go": "", "d/x.go": "", "e/x.go": "",
    })
    assert set(mapper.scan(repo).edges) == {("a", "c"), ("a", "d"), ("a", "e")}


def test_service_folders_found_by_image_or_suffix_and_db_kind(tmp_path):
    repo = make(tmp_path / "r", {
        "docker-compose.yml": ("services:\n  customers: {image: 'acme/acme-customers-service:1'}\n"
                               "  vets-service: {image: 'vets:1'}\n  mongodb: {build: ./mongo}\n"),
        "acme-customers-service/src/main/java/org/acme/samples/Repo.java": 'String u = "http://vets-service/vets";\n',
        "platform-vets-service/app.py": "",
        "mongo/Dockerfile": "FROM mongo",
        "examples/k8s/demo.yaml": deploy("example-only"),  # top-level examples are not part of the system
    })
    m = mapper.scan(repo)
    assert m.services["customers"]["source_dir"] == "acme-customers-service"   # matched by image name
    assert m.services["vets-service"]["source_dir"] == "platform-vets-service"  # matched by -<name> suffix
    assert m.services["mongodb"]["kind"] == "datastore"                         # an engine, even with its own image
    assert "example-only" not in m.services
    assert ("customers", "vets-service") in m.edges  # code deep in a .../samples/... package is still read


def test_config_file_named_after_a_service_belongs_to_it(tmp_path):
    repo = make(tmp_path / "r", {
        "docker-compose.yml": "services:\n  config: {build: ./config}\n  orders: {build: ./orders}\n  orders-db: {image: 'mongo'}\n",
        "config/shared/orders.yml": "spring:\n  data:\n    mongodb:\n      host: orders-db\n",
        "orders/app.py": "",
    })
    edges = set(mapper.scan(repo).edges)
    assert ("orders", "orders-db") in edges and ("config", "orders-db") not in edges


def test_dotnet_aspire_apphost(tmp_path):
    repo = make(tmp_path / "r", {
        "src/AppHost/Program.cs": (
            'var builder = DistributedApplication.CreateBuilder(args);\n'
            'var cache = builder.AddRedis("cache");\n'
            'var pg = builder.AddPostgres("pg");\nvar db = pg.AddDatabase("ordersdb");\n'
            'var api = builder.AddProject<Projects.Orders_Api>("orders-api")\n    .WithReference(db)\n    .WithReference(cache);\n'
            'builder.AddProject("web", "../Web").WithReference(api);\n'
            'builder.AddAzureContainerAppEnvironment("aca");\nbuilder.Build().Run();\n'),
        "src/Orders.Api/Program.cs": "", "src/Web/Program.cs": "",
    })
    m = mapper.scan(repo)
    assert {n: s["kind"] for n, s in m.services.items()} == {
        "cache": "datastore", "pg": "datastore", "ordersdb": "datastore", "orders-api": "service", "web": "service"}
    assert m.services["orders-api"]["source_dir"] == "src/Orders.Api" and m.services["web"]["source_dir"] == "src/Web"
    assert set(m.edges) == {("orders-api", "ordersdb"), ("orders-api", "cache"), ("web", "orders-api")}
