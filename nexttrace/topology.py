"""Online Boutique service map: the semantic memory every NextTrace graph starts from.

Edges come from the *_ADDR settings in GoogleCloudPlatform/microservices-demo kubernetes-manifests
and match docs/img/architecture-diagram.png. Node names match the RCAEval metric column prefixes.
owner_team, tier and user_function are synthetic (ours).
"""
import random
import string

# name: (owner_team, tier, user_function). Tier 1 = customers can't buy without it.
SERVICES = {
    "frontend": ("web-team", 1, "use the website"),
    "checkoutservice": ("orders-team", 1, "place orders"),
    "cartservice": ("orders-team", 1, "use their shopping cart"),
    "redis": ("platform-team", 1, "keep their cart saved"),
    "paymentservice": ("payments-team", 1, "pay"),
    "productcatalogservice": ("catalog-team", 1, "see products"),
    "currencyservice": ("payments-team", 2, "see prices in their currency"),
    "shippingservice": ("logistics-team", 2, "get shipping quotes"),
    "emailservice": ("notifications-team", 3, "get order confirmation emails"),
    "recommendationservice": ("growth-team", 3, "see recommendations"),
    "adservice": ("growth-team", 3, "see ads"),
}

# (caller, callee)
CALLS = [
    ("frontend", "adservice"),
    ("frontend", "recommendationservice"),
    ("frontend", "productcatalogservice"),
    ("frontend", "cartservice"),
    ("frontend", "shippingservice"),
    ("frontend", "currencyservice"),
    ("frontend", "checkoutservice"),
    ("checkoutservice", "productcatalogservice"),
    ("checkoutservice", "cartservice"),
    ("checkoutservice", "shippingservice"),
    ("checkoutservice", "currencyservice"),
    ("checkoutservice", "paymentservice"),
    ("checkoutservice", "emailservice"),
    ("recommendationservice", "productcatalogservice"),
    ("cartservice", "redis"),
]

LOAD_SERVICES = """
UNWIND $services AS s
MERGE (n:Service {name: s.name})
SET n.owner_team = s.owner_team, n.tier = s.tier, n.user_function = s.user_function
"""

LOAD_CALLS = """
UNWIND $calls AS c
MATCH (a:Service {name: c.caller}), (b:Service {name: c.callee})
MERGE (a)-[:CALLS]->(b)
"""


def anonymizer(seed: int = 7) -> dict[str, str]:
    """Fixed real -> svc-<letter> mapping, so an LLM can't use what it knows about Online Boutique."""
    names = sorted(SERVICES)
    letters = list(string.ascii_lowercase[: len(names)])
    random.Random(seed).shuffle(letters)
    return {n: f"svc-{l}" for n, l in zip(names, letters)}


def load(graph, rename: dict[str, str] | None = None) -> None:
    """MERGE the map into a FalkorDB graph. Idempotent. Pass anonymizer() to load anonymized names."""
    r = rename or {}
    graph.query(LOAD_SERVICES, {"services": [
        {"name": r.get(n, n), "owner_team": o, "tier": t, "user_function": f}
        for n, (o, t, f) in SERVICES.items()
    ]})
    graph.query(LOAD_CALLS, {"calls": [{"caller": r.get(a, a), "callee": r.get(b, b)} for a, b in CALLS]})
    # after the first write: FalkorDB rejects index calls on a graph that doesn't exist yet
    if not any("Service" in str(row) for row in graph.list_indices().result_set):
        graph.create_node_range_index("Service", "name")
