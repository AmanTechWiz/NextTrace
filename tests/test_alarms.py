"""Alarms from a wide metrics CSV: generic service names, baseline vs after, noise and bad input."""
import pandas as pd
import pytest

from nexttrace import alarms


def csv(tmp_path, cols):
    t = list(range(100, 200))
    df = pd.DataFrame({"time": t, **{k: [f(x) for x in t] for k, f in cols.items()}})
    p = tmp_path / "m.csv"
    df.to_csv(p, index=False)
    return p


def wobble(base, jump=0):
    return lambda t: base + (t % 3) * 0.1 + (jump if t >= 150 else 0)


def test_flags_moved_metrics_with_longest_service_prefix(tmp_path):
    p = csv(tmp_path, {"api-v2_latency-90": wobble(10, 50), "api_latency-90": wobble(10), "api_cpu": wobble(1, 5),
                       "mystery_cpu": wobble(1, 9), "db_conns": wobble(5, 0.05)})
    got = alarms.from_metrics(p, 150, ["api", "api-v2", "db"])
    assert [(a["service"], a["signal"]) for a in got] == [("api-v2", "latency-90"), ("api", "cpu")]  # strongest first
    assert all(a["at"] == 150 for a in got)
    assert alarms.from_metrics(p, 150, ["api", "api-v2"], top=1)[0]["service"] == "api-v2"


def test_flat_columns_and_bad_input(tmp_path):
    p = csv(tmp_path, {"api_flat": lambda t: 1.0, "api_text": lambda t: "n/a"})
    assert alarms.from_metrics(p, 150, ["api"]) == []
    with pytest.raises(ValueError, match="must fall inside"):
        alarms.from_metrics(p, 999, ["api"])
    bad = tmp_path / "bad.csv"
    pd.DataFrame({"ts": [1, 2]}).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="'time' column"):
        alarms.from_metrics(bad, 1, ["api"])
