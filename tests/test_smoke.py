"""Step S1: prove every FalkorDB feature NextTrace relies on, before building on it.

Needs FalkorDB on localhost:6379. Run: uv run pytest tests/test_smoke.py -v
"""
import asyncio
import time

import pytest
from falkordb import FalkorDB
from falkordb.asyncio import FalkorDB as AsyncFalkorDB
from redis.asyncio import BlockingConnectionPool

db = FalkorDB(host="localhost", port=6379)

CHAIN = """
CREATE (a:Service {name:'frontend'})-[:CALLS]->(b:Service {name:'checkout'}),
       (b)-[:CALLS]->(c:Service {name:'payment'}),
       (c)-[:CALLS]->(d:Service {name:'db'}),
       (a)-[:CALLS]->(:Service {name:'product'})
"""


@pytest.fixture
def g():
    graph = db.select_graph("smoke")
    if "smoke" in db.list_graphs():
        graph.delete()
    graph.query(CHAIN)
    yield graph
    graph.delete()


def names(rows):
    return sorted(r[0] for r in rows)


def test_variable_length_upstream_distinct(g):
    rows = g.ro_query(
        "MATCH (up:Service)-[:CALLS*1..6]->(:Service {name:$n}) RETURN DISTINCT up.name", {"n": "db"}
    ).result_set
    assert names(rows) == ["checkout", "frontend", "payment"]


def test_in_operator_and_list_comprehension(g):
    rows = g.ro_query(
        "MATCH (s:Service) WITH collect(s.name) AS all "
        "RETURN [x IN all WHERE x IN $bad] AS hit",
        {"bad": ["payment", "db", "nope"]},
    ).result_set
    assert sorted(rows[0][0]) == ["db", "payment"]


def test_where_not_pattern_predicate(g):
    # services that call nothing = leaves
    rows = g.ro_query("MATCH (s:Service) WHERE NOT (s)-[:CALLS]->() RETURN s.name").result_set
    assert names(rows) == ["db", "product"]


def test_shortest_path(g):
    rows = g.ro_query(
        "MATCH (a:Service {name:$a}), (b:Service {name:$b}) "
        "WITH shortestPath((a)-[:CALLS*]->(b)) AS p RETURN [n IN nodes(p) | n.name]",
        {"a": "frontend", "b": "db"},
    ).result_set
    assert rows[0][0] == ["frontend", "checkout", "payment", "db"]


def test_rca_candidates_pattern(g):
    # payment + checkout + frontend alarming; db fine -> payment is the deepest bad one
    rows = g.ro_query(
        """
        MATCH (s:Service) WHERE s.name IN $bad
        WITH collect(s) AS bad
        UNWIND bad AS s
        OPTIONAL MATCH (s)-[:CALLS*1..4]->(d:Service) WHERE d IN bad
        WITH s, bad, count(DISTINCT d) AS badDeps
        OPTIONAL MATCH (s)<-[:CALLS*1..4]-(u:Service) WHERE u IN bad
        RETURN s.name, badDeps, count(DISTINCT u) AS explains
        ORDER BY badDeps ASC, explains DESC
        """,
        {"bad": ["frontend", "checkout", "payment"]},
    ).result_set
    assert rows[0] == ["payment", 0, 2]


def test_merge_is_idempotent(g):
    for _ in range(2):
        g.query("MERGE (:Signal {key:$k})", {"k": "payment:cpu:1"})
    assert g.ro_query("MATCH (s:Signal) RETURN count(s)").result_set[0][0] == 1


def test_cas_handoff_only_one_wins(g):
    g.query("CREATE (:Incident {id:'i1', status:'open', version:0})")
    cas = (
        "MATCH (i:Incident {id:$id}) WHERE i.version = $v AND i.status = $from "
        "SET i.status = $to, i.version = $v + 1, i.owner_agent = $agent RETURN i.version"
    )
    p = {"id": "i1", "v": 0, "from": "open", "to": "triaging"}
    first = g.query(cas, {**p, "agent": "a"}).result_set
    second = g.query(cas, {**p, "agent": "b"}).result_set
    assert first == [[1]] and second == []


def unique_signal_key(g):
    g.create_node_range_index("Signal", "key")
    g.create_node_unique_constraint("Signal", "key")
    for _ in range(50):  # constraint builds asynchronously
        if "OPERATIONAL" in str(g.list_constraints()):
            return
        time.sleep(0.1)
    raise AssertionError(g.list_constraints())


def test_unique_constraint_rejects_duplicate(g):
    unique_signal_key(g)
    g.query("CREATE (:Signal {key:'x'})")
    with pytest.raises(Exception):
        g.query("CREATE (:Signal {key:'x'})")


def test_algorithms_callable(g):
    bfs = g.ro_query(
        "MATCH (s:Service {name:'frontend'}) CALL algo.bfs(s, 2, 'CALLS') YIELD nodes "
        "RETURN [n IN nodes | n.name]"
    ).result_set
    assert set(bfs[0][0]) >= {"checkout", "product", "payment"}
    pr = g.ro_query("CALL algo.pageRank('Service','CALLS') YIELD node, score RETURN node.name, score ORDER BY score DESC").result_set
    assert pr[0][0] == "db"  # most-depended-on leaf of the chain
    bt = g.ro_query(
        "CALL algo.betweenness({nodeLabels:['Service'], relationshipTypes:['CALLS'], samplingSeed:1}) "
        "YIELD node, score RETURN node.name, score ORDER BY score DESC"
    ).result_set
    assert bt[0][0] in {"checkout", "payment"}


def test_tenant_isolation():
    acme, globex = db.select_graph("smoke_acme"), db.select_graph("smoke_globex")
    try:
        acme.query("CREATE (:Incident {id:'secret'})")
        globex.query("CREATE (:Incident {id:'other'})")
        assert globex.ro_query("MATCH (i:Incident {id:'secret'}) RETURN i").result_set == []
    finally:
        acme.delete()
        globex.delete()


async def _parallel(n_writers, query, params_for):
    pool = BlockingConnectionPool(max_connections=n_writers, timeout=None, decode_responses=True)
    adb = AsyncFalkorDB(connection_pool=pool)
    ag = adb.select_graph("smoke")
    errors = []

    async def one(i):
        for attempt in range(20):
            try:
                return await ag.query(query, params_for(i))
            except Exception as e:
                # the image sets MAX_QUEUED_QUERIES 25: back off and retry, like a real writer would
                if "Max pending queries exceeded" in str(e):
                    await asyncio.sleep(0.005 * (attempt + 1))
                    continue
                errors.append(str(e))
                return
        errors.append("gave up after 20 retries")

    t0 = time.perf_counter()
    results = await asyncio.gather(*(one(i) for i in range(n_writers)))
    elapsed = time.perf_counter() - t0
    await pool.aclose()
    return errors, elapsed, results


def test_50_parallel_merges_make_one_node(g):
    # Without an index, parallel MERGE made duplicates in 16-21 of 30 runs (Oct 15 test).
    # Index + unique constraint is the guarantee; losers get a violation error = "already exists".
    unique_signal_key(g)
    errors, elapsed, _ = asyncio.run(
        _parallel(50, "MERGE (s:Signal {key:$k}) ON CREATE SET s.n = $i", lambda i: {"k": "same", "i": i})
    )
    print(f"\n50 parallel MERGE: {elapsed*1000:.1f} ms, errors={len(errors)} {errors[:1]}")
    assert all("unique constraint violation" in e for e in errors)
    assert g.ro_query("MATCH (s:Signal {key:'same'}) RETURN count(s)").result_set[0][0] == 1


def test_50_parallel_cas_one_winner(g):
    g.query("CREATE (:Incident {id:'i1', status:'open', version:0})")
    cas = ("MATCH (i:Incident {id:'i1'}) WHERE i.version = 0 AND i.status = 'open' "
           "SET i.status = 'triaging', i.version = 1, i.owner_agent = $a RETURN i.owner_agent")
    errors, _, results = asyncio.run(_parallel(50, cas, lambda i: {"a": f"agent{i}"}))
    assert errors == []
    winners = [r.result_set[0][0] for r in results if r.result_set]
    owner = g.ro_query("MATCH (i:Incident {id:'i1'}) RETURN i.owner_agent, i.version").result_set[0]
    assert len(winners) == 1 and owner == [winners[0], 1]


def test_50_parallel_increments_lose_nothing(g):
    g.query("CREATE (:Runbook {id:'r1', successes:0})")
    errors, elapsed, _ = asyncio.run(
        _parallel(50, "MATCH (r:Runbook {id:'r1'}) SET r.successes = r.successes + 1", lambda i: {})
    )
    print(f"\n50 parallel increments: {elapsed*1000:.1f} ms, errors={len(errors)} {errors[:1]}")
    assert errors == []
    assert g.ro_query("MATCH (r:Runbook {id:'r1'}) RETURN r.successes").result_set[0][0] == 50
