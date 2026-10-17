"""Incident memory and handoffs: triage, claims, findings, resolution, recall, history, concurrency, tenants."""
import threading

import pytest
from falkordb import FalkorDB

from nexttrace import memory as M
from nexttrace.graphdb import connect

GRAPH, OTHER = "test_memory", "test_memory_other"
# web -> gateway -> orders -> payments -> pay-db ; billing is unrelated ; old -> orders is stale
EDGES = [("web", "gateway", False), ("gateway", "orders", False), ("orders", "payments", False),
         ("payments", "pay-db", False), ("old", "orders", True), ("billing", "ledger", False)]


def build(name):
    g = FalkorDB().select_graph(name)
    if name in FalkorDB().list_graphs():
        g.delete()
    names = sorted({x for a, b, _ in EDGES for x in (a, b)})
    g.query("UNWIND $n AS n CREATE (:Service {name: n, kind: 'service'})", {"n": names})
    g.query("UNWIND $e AS e MATCH (a:Service {name: e.a}), (b:Service {name: e.b}) "
            "CREATE (a)-[:CALLS {stale: e.s, evidence: ['code | ' + e.a + '/client.py:1 | http://' + e.b]}]->(b)",
            {"e": [{"a": a, "b": b, "s": s} for a, b, s in EDGES]})
    return g


@pytest.fixture
def g():
    graph = build(GRAPH)
    yield graph
    graph.delete()


def investigate(g, inc, agent="inv", cause="payments", cited=(("orders", "payments"),)):
    M.claim(g, inc, agent, "open", "investigating")
    return M.record_finding(g, inc, agent, cause, "orders slow because payments times out", list(cited))


def test_triage_groups_by_the_map_and_is_idempotent(g):
    alarms = [{"service": "web", "signal": "latency", "at": 1000}, {"service": "payments", "signal": "errors", "at": 1000},
              {"service": "billing", "signal": "cpu", "at": 1000}]
    first = M.triage(g, alarms)
    assert sorted(i["services"] for i in first) == [["billing"], ["payments", "web"]]
    assert all(i["created"] for i in first)
    again = M.triage(g, alarms)
    assert sorted(i["incident"] for i in again) == sorted(i["incident"] for i in first)
    assert not any(i["created"] for i in again)
    assert g.ro_query("MATCH (a:Alarm) RETURN count(a)").result_set == [[3]]
    assert g.ro_query("MATCH (i:Incident) RETURN count(i)").result_set == [[2]]


def test_triage_rejects_bad_alarms(g):
    with pytest.raises(ValueError, match="no service 'ghost'"):
        M.triage(g, [{"service": "ghost", "signal": "cpu"}])
    with pytest.raises(ValueError, match="no signal"):
        M.triage(g, [{"service": "web", "signal": " "}])
    with pytest.raises(ValueError, match="no alarms"):
        M.triage(g, [])


def test_stale_links_and_hop_limit_split_groups(g):
    # old -> orders exists only as stale: old's alarm must not join the orders incident
    out = M.triage(g, [{"service": "old", "signal": "cpu", "at": 1}, {"service": "orders", "signal": "cpu", "at": 1}])
    assert sorted(i["services"] for i in out) == [["old"], ["orders"]]
    assert M.group_alarms(g, {"web", "pay-db"}, hops=3) == [["pay-db"], ["web"]]   # 4 hops apart
    assert M.group_alarms(g, {"web", "pay-db"}, hops=4) == [["pay-db", "web"]]


def test_full_handoff_flow_and_timeline(g):
    (inc,) = [i["incident"] for i in M.triage(g, [{"service": "orders", "signal": "latency", "at": 5}], agent="triage")]
    f = investigate(g, inc)
    assert f["evidence"] == ["orders -> payments | code | orders/client.py:1 | http://payments"]  # the map's evidence
    r = M.resolve(g, inc, "scribe", "restart payments pods", runbook="restart payments")
    assert (r["status"], r["attempts"], r["successes"]) == ("resolved", 1, 1)
    got = M.get_incident(g, inc)
    assert (got["status"], got["cause"], got["fix"]) == ("resolved", "payments", "restart payments pods")
    steps = [(e["agent"], e["action"], e["from"], e["to"]) for e in M.timeline(g, inc)]
    assert steps == [("triage", "opened", "", "open"), ("inv", "claimed", "open", "investigating"),
                     ("inv", "diagnosed", "investigating", "diagnosed"), ("scribe", "resolved", "diagnosed", "resolved")]


def test_rules_the_graph_enforces(g):
    (inc,) = [i["incident"] for i in M.triage(g, [{"service": "orders", "signal": "cpu", "at": 9}])]
    with pytest.raises(ValueError, match="flow is"):
        M.claim(g, inc, "x", "open", "resolved")                         # can't skip steps
    with pytest.raises(M.ConflictError, match="claim it first"):
        M.record_finding(g, inc, "x", "payments", "r", [])               # not claimed
    M.claim(g, inc, "inv", "open", "investigating")
    with pytest.raises(M.ConflictError, match="owned by inv"):
        M.record_finding(g, inc, "intruder", "payments", "r", [])        # someone else's incident
    with pytest.raises(ValueError, match="not on the map"):
        M.record_finding(g, inc, "inv", "payments", "r", [("web", "payments")])        # invented connection
    with pytest.raises(ValueError, match=r"\(stale\)"):
        M.record_finding(g, inc, "inv", "orders", "r", [("old", "orders")])            # gone from the code
    with pytest.raises(ValueError, match="no service 'ghost'"):
        M.record_finding(g, inc, "inv", "ghost", "r", [])
    with pytest.raises(M.ConflictError, match="only a diagnosed"):
        M.resolve(g, inc, "scribe", "fix")
    M.record_finding(g, inc, "inv", "payments", "r", [("orders", "payments")])
    with pytest.raises(ValueError, match="describe the fix"):
        M.resolve(g, inc, "scribe", "  ")
    failed = M.resolve(g, inc, "scribe", "scale up", worked=False, runbook="scale")
    assert failed["status"] == "diagnosed" and (failed["attempts"], failed["successes"]) == (1, 0)
    ok = M.resolve(g, inc, "scribe", "restart", runbook="restart")
    assert ok["status"] == "resolved"


def test_recall_finds_the_past_fix(g):
    (old,) = [i["incident"] for i in M.triage(g, [{"service": "orders", "signal": "latency", "at": 100},
                                                  {"service": "payments", "signal": "errors", "at": 100}])]
    investigate(g, old)
    M.resolve(g, old, "scribe", "roll back payments to v41", runbook="rollback payments")
    (new,) = [i["incident"] for i in M.triage(g, [{"service": "payments", "signal": "errors", "at": 99999}])]
    hits = M.similar_incidents(g, ["payments"], exclude=new)
    assert hits[0]["incident"] == old and hits[0]["fix"] == "roll back payments to v41" and hits[0]["worked"] == "1/1"
    assert M.similar_incidents(g, ["billing"]) == []                    # nothing similar: say so, don't guess
    boosted = M.similar_incidents(g, ["web"], cause="payments")
    assert boosted and boosted[0]["score"] == 0.5


def test_history_persists_across_connections(g):
    sid = M.start_session(g, "investigator", "INC-x")
    assert M.log_step(g, sid, "blast_radius", {"service": "payments"}, {"affected": 3}) == 1
    assert M.log_step(g, sid, "failure_paths", {"service": "payments"}, "2 paths") == 2
    with pytest.raises(ValueError, match="start one first"):
        M.log_step(g, "S-nope", "x")
    fresh = connect().select_graph(GRAPH)                                 # a new process / agent / day
    steps = M.history(fresh, agent="investigator")
    assert [s["tool"] for s in steps] == ["failure_paths", "blast_radius"] and steps[1]["input"] == '{"service": "payments"}'
    with pytest.raises(ValueError, match="give a session"):
        M.history(fresh)


def test_parallel_agents_converge(g):
    """30 triage agents on the same alarms open one incident; 20 investigators racing for it: exactly one wins."""
    alarms = [{"service": "orders", "signal": "latency", "at": 7}, {"service": "payments", "signal": "errors", "at": 7}]
    results, errors = [], []

    def tri():
        try:
            results.append(M.triage(connect().select_graph(GRAPH), alarms))
        except Exception as e:                                            # noqa: BLE001 - surface any failure
            errors.append(e)
    ts = [threading.Thread(target=tri) for _ in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert g.ro_query("MATCH (i:Incident) RETURN count(i)").result_set == [[1]]
    assert g.ro_query("MATCH (a:Alarm) RETURN count(a)").result_set == [[2]]
    inc = results[0][0]["incident"]
    wins, losses = [], []

    def grab(i):
        try:
            M.claim(connect().select_graph(GRAPH), inc, f"inv{i}", "open", "investigating", version=0)
            wins.append(i)
        except M.ConflictError:
            losses.append(i)
    ts = [threading.Thread(target=grab, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1 and len(losses) == 19
    assert M.get_incident(g, inc)["owner"] == f"inv{wins[0]}"


def test_runbook_counts_lose_nothing_under_parallel_resolves(g):
    incs = [M.triage(g, [{"service": "orders", "signal": f"s{i}", "at": 1}])[0]["incident"] for i in range(10)]
    for inc in incs:
        investigate(g, inc)
    ts = [threading.Thread(target=M.resolve, args=(connect().select_graph(GRAPH), inc, "scribe", "restart"),
                           kwargs={"runbook": "restart payments"}) for inc in incs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert g.ro_query("MATCH (r:Runbook {key: 'restart payments'}) RETURN r.attempts, r.successes").result_set == [[10, 10]]


def test_tenants_are_isolated(g):
    other = build(OTHER)
    try:
        (a,) = [i["incident"] for i in M.triage(g, [{"service": "orders", "signal": "cpu", "at": 1}])]
        investigate(g, a)
        M.resolve(g, a, "scribe", "team A's private fix")
        M.start_session(g, "agent-a", "secret")
        assert M.similar_incidents(other, ["orders"]) == []
        assert M.history(other, agent="agent-a") == []
        with pytest.raises(ValueError, match="no incident"):
            M.get_incident(other, a)
    finally:
        other.delete()


def test_stale_view_is_refused_even_if_status_looks_the_same(g):
    """A failed fix leaves the incident 'diagnosed' again but at a new version: an agent acting on what it read before
    the failed attempt must be refused, not allowed to close it."""
    (inc,) = [i["incident"] for i in M.triage(g, [{"service": "orders", "signal": "cpu", "at": 3}])]
    investigate(g, inc)
    seen = M.get_incident(g, inc)["version"]                       # scribe B reads the incident
    M.resolve(g, inc, "scribe-a", "scale up", worked=False)        # scribe A tries a fix that fails
    assert M.get_incident(g, inc)["status"] == "diagnosed"
    with pytest.raises(M.ConflictError, match=f"not diagnosed at version {seen}"):
        M.claim(g, inc, "scribe-b", "diagnosed", "resolved", version=seen)
