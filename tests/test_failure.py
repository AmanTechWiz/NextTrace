"""Failure tools: blast radius, failure paths, criticality.

Checked against an independent plain-Python reference (reverse BFS over the same edges), not against the queries
themselves, on a small map built to contain the traps: a cycle, a stale shortcut, tied shortest paths, a weak
(agent-inferred) link, a datastore, an isolated service. Every cited connection is also checked to exist as stored.
"""
from collections import deque
from pathlib import Path

import pytest
from falkordb import FalkorDB

from nexttrace import failure, mapper

GRAPH = "test_failure"
REPOS = Path(__file__).parent.parent / "data" / "repos"

SERVICES = {  # name: (kind, owner)
    "web": ("service", "web-team"), "admin": ("service", "ops"), "legacy": ("service", ""), "lonely": ("service", ""),
    "gateway": ("service", "platform"), "catalog": ("service", "catalog-team"), "search": ("service", "search-team"),
    "orders": ("service", "orders-team"), "inventory": ("service", "orders-team"), "payments": ("service", "pay-team"),
    "pay-db": ("datastore", "pay-team"), "catalog-db": ("datastore", "catalog-team"),
}
EDGES = [  # caller, callee, confidence, stale
    ("web", "gateway", "high", False), ("web", "catalog", "high", False),
    ("gateway", "orders", "medium", False), ("gateway", "catalog", "high", False), ("gateway", "search", "medium", False),
    ("catalog", "search", "high", False), ("catalog", "catalog-db", "high", False),
    ("orders", "payments", "high", False), ("orders", "inventory", "medium", False),
    ("inventory", "orders", "medium", False),                     # cycle
    ("payments", "pay-db", "high", False),
    ("admin", "orders", "inferred", False),                       # added by an agent
    ("legacy", "payments", "high", False),
    ("web", "payments", "high", True),                            # stale shortcut: gone from the code
]


@pytest.fixture
def g():
    db = FalkorDB()
    graph = db.select_graph(GRAPH)
    if GRAPH in db.list_graphs():
        graph.delete()
    graph.query("UNWIND $rows AS r CREATE (:Service {name: r.n, kind: r.k, owner: r.o, stale: false})",
                {"rows": [{"n": n, "k": k, "o": o} for n, (k, o) in SERVICES.items()]})
    graph.query("UNWIND $rows AS r MATCH (a:Service {name: r.a}), (b:Service {name: r.b}) "
                "CREATE (a)-[:CALLS {confidence: r.c, sources: r.src, evidence: [r.ev], stale: r.s}]->(b)",
                {"rows": [{"a": a, "b": b, "c": c, "s": s, "src": ["ai"] if c == "inferred" else ["code"],
                           "ev": f"code | {a}/client.py:{i + 1} | http://{b}/api"} for i, (a, b, c, s) in enumerate(EDGES)]})
    yield graph
    graph.delete()


# ---------- independent reference ----------

def ref_depths(target, include_stale=False, max_depth=6):
    """Shortest number of hops from each caller to `target`, by reverse BFS."""
    callers = {}
    for a, b, _, stale in EDGES:
        if include_stale or not stale:
            callers.setdefault(b, set()).add(a)
    depth, q = {}, deque([(target, 0)])
    while q:
        node, d = q.popleft()
        if d == max_depth:
            continue
        for c in callers.get(node, ()):
            if c != target and c not in depth:
                depth[c] = d + 1
                q.append((c, d + 1))
    return depth


def assert_cited_and_real(g, hops_list):
    stored = {(a, b): (c, s) for a, b, c, s in EDGES}
    for hops in hops_list:
        for h in hops:
            assert (h["caller"], h["callee"]) in stored, h
            assert h["evidence"].startswith("code | ") and h["confidence"] == stored[(h["caller"], h["callee"])][0]


# ---------- blast radius ----------

@pytest.mark.parametrize("target", sorted(SERVICES))
def test_blast_radius_matches_reference_for_every_service(g, target):
    got = failure.blast_radius(g, target)
    assert {a["service"]: a["depth"] for a in got["affected"]} == ref_depths(target)
    for a in got["affected"]:
        assert a["path"][0] == a["service"] and a["path"][-1] == target and len(a["path"]) == a["depth"] + 1
        assert [h["caller"] for h in a["hops"]] == a["path"][:-1] and [h["callee"] for h in a["hops"]] == a["path"][1:]
        assert not any(h["stale"] for h in a["hops"])
    assert_cited_and_real(g, [a["hops"] for a in got["affected"]])
    assert {(c["caller"], c["callee"]) for c in got["cited_connections"]} == {
        (h["caller"], h["callee"]) for a in got["affected"] for h in a["hops"]}


def test_blast_radius_details(g):
    got = failure.blast_radius(g, "payments")
    by = {a["service"]: a for a in got["affected"]}
    assert got["direct_callers"] == ["legacy", "orders"]                       # stale web -> payments left out
    assert by["web"]["depth"] == 3 and by["web"]["path"] == ["web", "gateway", "orders", "payments"]
    assert by["web"]["confidence"] == "medium"                                  # weakest hop on the chain
    assert by["admin"]["confidence"] == "inferred"                              # rests on an agent-added link
    assert got["entry_points_hit"] == ["admin", "legacy", "web"]
    assert set(got["owners_to_notify"]) == {"pay-team", "orders-team", "platform", "web-team", "ops"}
    assert got["by_depth"] == {1: 2, 2: 3, 3: 1}


def test_blast_radius_stale_depth_cycle_and_isolated(g):
    with_stale = failure.blast_radius(g, "payments", include_stale=True)
    assert {a["service"]: a["depth"] for a in with_stale["affected"]}["web"] == 1
    assert {a["service"]: a["depth"] for a in with_stale["affected"]} == ref_depths("payments", include_stale=True)
    shallow = failure.blast_radius(g, "payments", max_depth=1)
    assert sorted(a["service"] for a in shallow["affected"]) == ["legacy", "orders"]
    cyc = failure.blast_radius(g, "orders")
    assert "orders" not in {a["service"] for a in cyc["affected"]} and {"inventory", "gateway", "admin", "web"} <= {
        a["service"] for a in cyc["affected"]}
    alone = failure.blast_radius(g, "lonely")
    assert alone["affected"] == [] and alone["cited_connections"] == [] and alone["affected_count"] == 0
    db = failure.blast_radius(g, "pay-db")
    assert db["kind"] == "datastore" and db["direct_callers"] == ["payments"]


def test_blast_radius_bad_input(g):
    with pytest.raises(ValueError, match="did you mean.*payments"):
        failure.blast_radius(g, "payment")
    for bad in (0, 11, -3):
        with pytest.raises(ValueError, match="max_depth"):
            failure.blast_radius(g, "payments", max_depth=bad)
    with pytest.raises(ValueError, match="no service"):
        failure.blast_radius(g, "x' OR 1=1 //")                                 # parameters, not string-built


# ---------- failure paths ----------

def test_paths_from_entry_points_keep_ties(g):
    got = failure.failure_paths(g, "search")
    assert got["from"] == ["admin", "legacy", "lonely", "web"]
    assert sorted(p["path"] for p in got["paths"]) == [["web", "catalog", "search"], ["web", "gateway", "search"]]
    assert {p["length"] for p in got["paths"]} == {2}
    assert got["not_reached"] == ["admin", "legacy", "lonely"]
    assert_cited_and_real(g, [p["hops"] for p in got["paths"]])
    assert {p["confidence"] for p in got["paths"]} == {"high", "medium"}


def test_paths_from_a_service_skip_stale_unless_asked(g):
    live = failure.failure_paths(g, "payments", from_service="web")
    assert [p["path"] for p in live["paths"]] == [["web", "gateway", "orders", "payments"]]
    stale = failure.failure_paths(g, "payments", from_service="web", include_stale=True)
    assert [p["path"] for p in stale["paths"]] == [["web", "payments"]] and stale["paths"][0]["hops"][0]["stale"]
    none = failure.failure_paths(g, "web", from_service="payments")
    assert none["paths"] == [] and none["not_reached"] == ["payments"]
    capped = failure.failure_paths(g, "search", max_paths=1)
    assert len(capped["paths"]) == 1 and capped["truncated"] and capped["path_count"] == 2


def test_paths_bad_input(g):
    with pytest.raises(ValueError, match="the same"):
        failure.failure_paths(g, "orders", from_service="orders")
    with pytest.raises(ValueError, match="no service 'nope'"):
        failure.failure_paths(g, "orders", from_service="nope")
    with pytest.raises(ValueError, match="max_depth"):
        failure.failure_paths(g, "orders", max_depth=99)


# ---------- criticality ----------

def test_criticality_ranks_by_dependents_and_cites_callers(g):
    got = failure.criticality(g, top=len(SERVICES))
    by = {r["service"]: r for r in got["ranked"]}
    for name in SERVICES:
        assert by[name]["dependents"] == len(ref_depths(name, max_depth=10)), name
    ranks = [r["rank"] for r in got["ranked"]]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)
    keys = [(-r["dependents"], -r["betweenness"], -r["pagerank"]) for r in got["ranked"]]
    assert keys == sorted(keys)
    assert by["lonely"]["dependents"] == 0 and by["lonely"]["betweenness"] == 0
    assert by["orders"]["betweenness"] > 0 and by["web"]["betweenness"] == 0    # orders is a bridge; web is an edge
    assert sorted(c["caller"] for c in by["payments"]["direct_callers"]) == ["legacy", "orders"]  # stale caller out
    assert_cited_and_real(g, [r["direct_callers"] for r in got["ranked"]])
    assert "1 stale connection" in got["note"]
    assert got["algorithms"][0] == "algo.pageRank"


def test_criticality_is_repeatable_and_filters(g):
    a, b = failure.criticality(g, top=5), failure.criticality(g, top=5)
    assert [(r["service"], r["betweenness"], r["pagerank"]) for r in a["ranked"]] == \
           [(r["service"], r["betweenness"], r["pagerank"]) for r in b["ranked"]]
    assert len(a["ranked"]) == 5
    one = failure.criticality(g, service="payments")
    assert [r["service"] for r in one["ranked"]] == ["payments"] and one["ranked"][0]["rank"] >= 1
    stores = failure.criticality(g, kind="datastore")
    assert {r["service"] for r in stores["ranked"]} == {"pay-db", "catalog-db"}
    with pytest.raises(ValueError, match="kind must be"):
        failure.criticality(g, kind="database")
    with pytest.raises(ValueError, match="no service"):
        failure.criticality(g, service="ghost")


def test_criticality_betweenness_matches_falkordb_documented_example():
    """FalkorDB's own betweenness example (docs.falkordb.com/algorithms/betweenness-centrality): Bob 6, Charlie 2,
    David 2, Alice 0, Emma 0. Its scale differs from textbook Brandes, so we pin FalkorDB's documented values: this
    fails if the call ever becomes sampled/approximate or targets the wrong labels."""
    db = FalkorDB()
    graph = db.select_graph("test_failure_doc")
    if "test_failure_doc" in db.list_graphs():
        graph.delete()
    graph.query("CREATE (a:Service {name: 'Alice'}), (b:Service {name: 'Bob'}), (c:Service {name: 'Charlie'}), "
                "(d:Service {name: 'David'}), (e:Service {name: 'Emma'}), (:Endpoint {name: 'noise'}), "
                "(a)-[:CALLS]->(b), (b)-[:CALLS]->(c), (b)-[:CALLS]->(d), (c)-[:CALLS]->(e), (d)-[:CALLS]->(e)")
    try:
        got = {r["service"]: r["betweenness"] for r in failure.criticality(graph, top=10)["ranked"]}
        assert got == {"Bob": 6, "Charlie": 2, "David": 2, "Alice": 0, "Emma": 0}
    finally:
        graph.delete()


def test_criticality_on_empty_map():
    db = FalkorDB()
    graph = db.select_graph("test_failure_empty")
    graph.query("CREATE (:Other {x: 1})")
    try:
        with pytest.raises(ValueError, match="no services"):
            failure.criticality(graph)
    finally:
        graph.delete()


# ---------- real repo ----------

@pytest.mark.skipif(not (REPOS / "train-ticket").exists(), reason="clone train-ticket v0.2.1 into data/repos/train-ticket")
def test_train_ticket_blast_radius_matches_the_scanned_edges():
    import time
    db = FalkorDB()
    graph = db.select_graph("test_failure_tt")
    if "test_failure_tt" in db.list_graphs():
        graph.delete()
    m = mapper.scan(REPOS / "train-ticket")
    mapper.write(graph, m)
    try:
        callers = {}
        for a, b in m.edges:
            callers.setdefault(b, set()).add(a)
        depth, q = {}, deque([("ts-order-service", 0)])
        while q:
            node, d = q.popleft()
            for c in callers.get(node, ()):
                if c != "ts-order-service" and c not in depth and d < 6:
                    depth[c] = d + 1
                    q.append((c, d + 1))
        t0 = time.perf_counter()
        got = failure.blast_radius(graph, "ts-order-service")
        assert time.perf_counter() - t0 < 2
        assert {a["service"]: a["depth"] for a in got["affected"]} == depth
        assert all(h["evidence"] for a in got["affected"] for h in a["hops"])
        crit = failure.criticality(graph, top=3, kind="service")
        assert all(r["kind"] == "service" for r in crit["ranked"]) and crit["ranked"][0]["dependents"] > 10
    finally:
        graph.delete()


def test_check_answer_verifies_live_calls_and_flags_the_rest(g):
    got = failure.check_answer(g, [{"caller": "orders", "callee": "payments"}, ("legacy", "payments"),
                                   ("web", "payments"),            # stale: gone from the code
                                   ("search", "payments"),         # both real services, no such call
                                   ("ghost", "payments"),          # invented service
                                   ("orders", "payments")])        # duplicate counted once
    assert [(v["caller"], v["callee"]) for v in got["verified"]] == [("orders", "payments"), ("legacy", "payments")]
    assert got["verified"][0]["evidence"] == "code | orders/client.py:8 | http://payments/api"   # the map's, not the agent's
    assert [(s["caller"], s["callee"]) for s in got["stale"]] == [("web", "payments")]
    assert got["not_on_map"] == [{"caller": "search", "callee": "payments"}, {"caller": "ghost", "callee": "payments"}]
    assert got["unknown_services"] == ["ghost"] and not got["ok"]
    assert got["summary"].startswith("2 verified, 4 flagged")


def test_check_answer_needs_every_claimed_service_chained_to_the_target(g):
    chain = [("orders", "payments"), ("gateway", "orders"), ("web", "gateway")]
    ok = failure.check_answer(g, chain, services=["orders", "gateway", "web"], target="payments")
    assert ok["ok"] and ok["summary"] == "3 verified, 0 flagged"
    # admin really calls orders, but the answer never cited that hop; catalog is real but doesn't reach payments
    got = failure.check_answer(g, chain[:2], services=["orders", "gateway", "web", "admin", "catalog"], target="payments")
    assert got["unsupported_services"] == ["admin", "catalog", "web"] and not got["ok"]
    # a broken middle hop leaves everything above it unsupported
    got = failure.check_answer(g, [("orders", "payments"), ("web", "gateway")], services=["orders", "web"], target="payments")
    assert got["unsupported_services"] == ["web"]


def test_check_answer_edge_cases(g):
    assert failure.check_answer(g, [])["ok"]
    assert failure.check_answer(g, [], services=["payments"])["ok"]
    assert failure.check_answer(g, [], services=["nope"])["unknown_services"] == ["nope"]
    with pytest.raises(ValueError, match="no service"):
        failure.check_answer(g, [], services=["orders"], target="paymnts")
    with pytest.raises(ValueError, match="caller and a callee"):
        failure.check_answer(g, [{"caller": "orders"}])


def test_check_answer_checks_quoted_code_in_the_mapped_repo(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "client.py").write_text("import os\n\nURL = 'http://orders/api'\n")
    db = FalkorDB()
    graph = db.select_graph("test_check_quotes")
    if "test_check_quotes" in db.list_graphs():
        graph.delete()
    try:
        graph.query("CREATE (:MapRun {root: $r}), (:Service {name: 'web'})-[:CALLS {confidence: 'high', "
                    "evidence: ['code | web/client.py:3 | x'], stale: false}]->(:Service {name: 'orders'})", {"r": str(tmp_path)})
        good = {"caller": "web", "callee": "orders", "file": "web/client.py", "line": 3, "quote": "URL = 'http://orders/api'"}
        assert failure.check_answer(graph, [good])["ok"]
        wrong_line = dict(good, line=40)                                  # real call, invented location
        made_up = dict(good, quote="OrdersClient.post('/api')")           # real call, invented code
        outside = dict(good, file="../../etc/passwd", line=1)
        got = failure.check_answer(graph, [wrong_line, made_up, outside])
        assert len(got["verified"]) == 1 and len(got["bad_evidence"]) == 3 and not got["ok"]
        assert "outside" in got["bad_evidence"][2]["reason"]
    finally:
        graph.delete()
