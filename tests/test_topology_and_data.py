import pytest
from falkordb import FalkorDB

from nexttrace import data, topology


def test_map_is_consistent():
    names = set(topology.SERVICES)
    assert len(topology.CALLS) == 15 and len(set(topology.CALLS)) == 15
    assert all(a in names and b in names for a, b in topology.CALLS)


def test_anonymizer_is_fixed_and_one_to_one():
    m = topology.anonymizer()
    assert m == topology.anonymizer()
    assert set(m) == set(topology.SERVICES) and len(set(m.values())) == len(m)


@pytest.mark.skipif(not data.DATA_DIR.exists(), reason="RE1-OB not downloaded")
def test_dataset_matches_map():
    ids = data.case_ids()
    assert len(ids) == 125
    roots = {i.split("/")[0].rsplit("_", 1)[0] for i in ids}
    assert roots <= set(topology.SERVICES)
    case = data.load_case("checkoutservice_cpu/4")  # the odd-length one
    assert "time.1" not in case.metrics.columns
    assert len(case.before()) == 2100
    in_map = {data.metric_service(c) for c in case.metrics.columns[1:]} & set(topology.SERVICES)
    assert in_map == set(topology.SERVICES)  # every map node has metrics


def test_load_into_falkordb_is_idempotent():
    db = FalkorDB()
    g = db.select_graph("test_topology")
    try:
        for _ in range(2):
            topology.load(g, topology.anonymizer())
        counts = g.ro_query("MATCH (s:Service) WITH count(s) AS n MATCH ()-[r:CALLS]->() RETURN n, count(r)").result_set
        assert counts == [[11, 15]]
        rows = g.ro_query("MATCH (s:Service) WHERE s.name STARTS WITH 'svc-' RETURN count(s)").result_set
        assert rows == [[11]]
    finally:
        g.delete()
