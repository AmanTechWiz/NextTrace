"""Generalization test: the mapper on repos it was never developed on, scored against their authors' own docs.

Protocol (so the score means something):
  1. The mapper is frozen at FROZEN before these repos are run.
  2. The answer key below was written from each repo's README / architecture diagram only, before running the mapper.
  3. Score once. No mapper change is made to improve these numbers; a later fix needs a fresh set of repos.

  uv run python -m bench.generalization        # clones nothing; expects repos in data/eval/<name>

Edges:
  sync:  A calls B directly (HTTP/gRPC).
  async: A and B talk through a broker. A static map can only show both using the broker, so it counts when
         both A and B connect to a broker node.
  store: A uses a datastore. The docs' name (e.g. "Order DB") rarely matches the deploy name, so it counts when
         A connects to any datastore node.
Names are the repos' own deploy/folder names; `doc` cites where each fact comes from.
"""
import json
import re
from pathlib import Path

from nexttrace import mapper

ROOT = Path(__file__).parent.parent
FROZEN = "c8078be"
BROKER = re.compile(r"rabbit|kafka|nats|pubsub|queue|servicebus|eventbus|redis")

KEY = {
    "aks-store-demo": {
        "url": "https://github.com/Azure-Samples/aks-store-demo",
        "doc": "README 'Architecture' + assets/demo-arch-with-openai.png",
        "sync": [("store-front", "order-service"), ("store-front", "product-service"),
                 ("store-admin", "product-service"), ("store-admin", "makeline-service"), ("store-admin", "ai-service")],
        "async": [("order-service", "makeline-service")],
        "store": ["makeline-service"],
    },
    "go-coffeeshop": {
        "url": "https://github.com/thangchung/go-coffeeshop",
        "doc": "docs/coffeeshop.svg (gRPC-gateway is the compose service `proxy`)",
        "sync": [("web", "proxy"), ("proxy", "counter"), ("proxy", "product"), ("counter", "product")],
        "async": [("counter", "barista"), ("counter", "kitchen")],
        "store": ["counter", "barista", "kitchen"],
    },
    "acme_fitness_demo": {
        "url": "https://github.com/vmwarecloudadvocacy/acme_fitness_demo",
        "doc": "acmeshop.png",
        "sync": [("front-end", "user"), ("front-end", "catalog"), ("front-end", "cart"), ("front-end", "order"),
                 ("catalog", "cart"), ("cart", "order"), ("order", "payment")],
        "async": [],
        "store": ["user", "catalog", "cart", "order"],
    },
    "reddog-code": {
        "url": "https://github.com/Azure/reddog-code",
        "doc": "README 'Architecture Diagram and Service Descriptions' + assets/reddog_code.png",
        "sync": [("ui", "order-service"), ("virtual-customers", "order-service"), ("virtual-worker", "make-line-service")],
        "async": [("order-service", "make-line-service"), ("order-service", "loyalty-service"),
                  ("order-service", "receipt-generation-service"), ("order-service", "accounting-service")],
        "store": ["make-line-service", "loyalty-service", "accounting-service"],
    },
    "microservice-kubernetes": {
        "url": "https://github.com/ewolff/microservice-kubernetes",
        "doc": "README: order 'uses catalog and customer'; Apache 'forwards HTTP requests to the microservices'",
        "sync": [("order", "catalog"), ("order", "customer"),
                 ("apache", "catalog"), ("apache", "customer"), ("apache", "order")],
        "async": [],
        "store": [],
    },
    "ftgo-application": {
        "url": "https://github.com/microservices-patterns/ftgo-application",
        "doc": "README.adoc ch.3-4: services talk over Kafka; CreateOrderSaga commands consumer, kitchen, accounting",
        "sync": [],
        "async": [("ftgo-order-service", "ftgo-consumer-service"), ("ftgo-order-service", "ftgo-kitchen-service"),
                  ("ftgo-order-service", "ftgo-accounting-service")],
        "store": [],
    },
    "yas": {
        "url": "https://github.com/nashtech-garage/yas",
        "doc": "yas-architecture-local.png (Storefront.BFF = `storefront`, Backoffice.BFF = `backoffice`)",
        "sync": [("nginx", "identity"), ("nginx", "storefront"), ("nginx", "backoffice"),
                 ("storefront", "storefront-nextjs"), ("backoffice", "backoffice-nextjs")]
                + [("nginx", s) for s in ("media", "product", "cart", "order", "rating", "customer",
                                          "location", "inventory", "tax", "search")],
        "async": [],
        "store": ["media", "product", "cart", "order", "rating", "customer", "location", "inventory", "tax"],
    },
    "eventuate-tram-sagas-examples-customers-and-orders": {
        "url": "https://github.com/eventuate-tram/eventuate-tram-sagas-examples-customers-and-orders",
        "doc": "images/Eventuate_Tram_Customer_and_Order_Orchestration_Architecture.png",
        "sync": [("api-gateway-service", "customer-service"), ("api-gateway-service", "order-service")],
        "async": [("order-service", "customer-service")],
        "store": ["customer-service", "order-service"],
    },
}


def score(name, key):
    m = mapper.scan(ROOT / "data" / "eval" / name)
    edges = set(m.edges)
    kinds = {n: s["kind"] for n, s in m.services.items()}
    nbrs = {}
    for a, b in edges:
        nbrs.setdefault(a, set()).add(b)
    brokers = {n for n, k in kinds.items() if k != "service" and BROKER.search(n)}
    stores = {n for n, k in kinds.items() if k == "datastore"}
    named = {a for a, _ in key["sync"] + key["async"]} | {b for _, b in key["sync"] + key["async"]} | set(key["store"])
    sync_hit = [e for e in key["sync"] if e in edges]
    async_hit = [e for e in key["async"] if nbrs.get(e[0], set()) & brokers and nbrs.get(e[1], set()) & brokers]
    store_hit = [s for s in key["store"] if nbrs.get(s, set()) & stores]
    apps = {n for n, k in kinds.items() if k == "service"}
    extra = sorted(e for e in edges if e[0] in apps and e[1] in apps and e not in key["sync"])
    return {
        "repo": name, "url": key["url"], "doc": key["doc"], "commit": m.commit,
        "services_named_in_docs": len(named), "services_found": len(named & set(m.services)),
        "services_missing": sorted(named - set(m.services)),
        "sync": [len(sync_hit), len(key["sync"])], "async": [len(async_hit), len(key["async"])],
        "store": [len(store_hit), len(key["store"])],
        "missed": [list(e) for e in key["sync"] if e not in sync_hit] + [list(e) + ["async"] for e in key["async"] if e not in async_hit]
                  + [[s, "store"] for s in key["store"] if s not in store_hit],
        "not_in_docs": [list(e) for e in extra],
    }


def frac(x):
    return f"{x[0]}/{x[1]}" if x[1] else "–"


def render(rows):
    tot = lambda k: [sum(r[k][0] for r in rows), sum(r[k][1] for r in rows)]
    svc = [sum(r["services_found"] for r in rows), sum(r["services_named_in_docs"] for r in rows)]
    allx = [a + b + c for a, b, c in zip(tot("sync"), tot("async"), tot("store"))]
    L = ["# Generalization: the mapper on repos it was never built on", "",
         f"Mapper frozen at commit `{FROZEN}` before these {len(rows)} repos were run. The answer key was written from each",
         "repo's own README and architecture diagram before running, and the result is reported as it came out:",
         "no mapper change was made to improve these numbers.", "",
         "- **sync**: A calls B directly. **async**: A and B share a broker (a static map can only show both using it).",
         "  **store**: A uses a datastore.",
         "- \"Not in docs\" are calls the mapper found that the docs don't draw. Diagrams are often simplified, so these",
         "  are not counted as wrong, but they are not counted as right either.", "",
         "| Repo | Services found | Direct calls | Via broker | Datastore | Not in docs |", "|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| [{r['repo']}]({r['url']}) | {r['services_found']}/{r['services_named_in_docs']} | {frac(r['sync'])} | "
                 f"{frac(r['async'])} | {frac(r['store'])} | {len(r['not_in_docs'])} |")
    L.append(f"| **Total** | **{frac(svc)}** | **{frac(tot('sync'))}** | **{frac(tot('async'))}** | **{frac(tot('store'))}** | "
             f"{sum(len(r['not_in_docs']) for r in rows)} |")
    L += ["", f"**Overall: {allx[0]}/{allx[1]} documented connections found ({100 * allx[0] / allx[1]:.0f}%).**", "",
          "## What was missed", ""]
    for r in rows:
        if r["missed"] or r["services_missing"]:
            L.append(f"- **{r['repo']}** ({r['doc']}): missed " + ", ".join(
                f"`{' → '.join(x[:2])}`" + (f" ({x[2]})" if len(x) > 2 else "") if len(x) > 1 else f"`{x[0]}`"
                for x in r["missed"]) + (f"; services not found: {r['services_missing']}" if r["services_missing"] else ""))
    L += ["", "## Found but not in the docs", ""]
    for r in rows:
        if r["not_in_docs"]:
            L.append(f"- **{r['repo']}**: " + ", ".join(f"`{a} → {b}`" for a, b in r["not_in_docs"]))
    return "\n".join(L) + "\n"


def main():
    rows = [score(n, k) for n, k in KEY.items()]
    out = ROOT / "bench" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / "generalization.json").write_text(json.dumps(rows, indent=2) + "\n")
    (ROOT / "docs" / "generalization.md").write_text(render(rows))
    for r in rows:
        print(f"{r['repo']:52s} services {r['services_found']}/{r['services_named_in_docs']} | sync {frac(r['sync'])} | "
              f"async {frac(r['async'])} | store {frac(r['store'])} | not-in-docs {len(r['not_in_docs'])}")


if __name__ == "__main__":
    main()
