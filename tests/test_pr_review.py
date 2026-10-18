"""PR review: real git repo -> real map -> real diff. Gemini is faked; no request leaves the machine.

Repo: web -> orders (POST /orders) -> payments (POST /pay); a leaf `audit` service nothing calls; a docs folder.
"""
import json
import subprocess
import urllib.error

import pytest
from falkordb import FalkorDB

from nexttrace import mapper, pr_review

GRAPH = "test_pr_review"
COMPOSE = """services:
  web: {build: ./web}
  orders: {build: ./orders}
  payments: {build: ./payments}
  audit: {build: ./audit}
"""
FILES = {
    "docker-compose.yml": COMPOSE,
    "web/Dockerfile": "FROM python:3.12\n", "orders/Dockerfile": "FROM python:3.12\n",
    "payments/Dockerfile": "FROM python:3.12\n", "audit/Dockerfile": "FROM python:3.12\n",
    "web/app.py": "import requests\n\ndef buy():\n    return requests.post('http://orders:5000/orders')\n",
    "orders/app.py": ("from flask import Flask\nimport requests\napp = Flask(__name__)\n\n"
                      "@app.route('/orders', methods=['POST'])\ndef create():\n"
                      "    return requests.post('http://payments:5000/pay').text\n"),
    "payments/app.py": ("from flask import Flask\napp = Flask(__name__)\n\n"
                        "@app.route('/pay', methods=['POST'])\ndef pay():\n    return 'ok'\n"),
    "audit/app.py": "def log(x):\n    print(x)\n",
    "docs/guide.md": "# guide\n",
}


def git(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    for p, text in FILES.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(text)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    db = FalkorDB()
    if GRAPH in db.list_graphs():
        db.select_graph(GRAPH).delete()
    mapper.map_repo(tmp_path, GRAPH)
    yield tmp_path, db.select_graph(GRAPH)
    db.select_graph(GRAPH).delete()


def change(root, path, text):
    git(root, "checkout", "-q", "-B", "pr", "main")
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "change")
    mapper.map_repo(root, GRAPH)  # like CI: the review reads the map of the PR head
    return [x for x in git(root, "diff", "--name-only", "main...pr").splitlines() if x], git(root, "diff", "main...pr")


def add_incident(g, cause):
    g.query("MATCH (c:Service {name: $c}) CREATE (i:Incident {id: 'INC-7', status: 'resolved', title: 'payments 5xx', "
            "fix: 'roll back pool size', resolved_at: 1})-[:CAUSED_BY]->(c), (i)-[:AFFECTS]->(c), "
            "(i)-[:FIXED_BY {worked: true}]->(:Runbook {key: 'rb-pool', attempts: 2, successes: 2})", {"c": cause})


def test_map_is_what_the_review_assumes(repo):
    _, g = repo
    calls = {tuple(r) for r in g.ro_query("MATCH (a:Service)-[:CALLS]->(b:Service) RETURN a.name, b.name").result_set}
    assert {("web", "orders"), ("orders", "payments")} <= calls


def test_changed_files_map_to_services_and_unowned(repo):
    _, g = repo
    hit, unowned = pr_review.changed_services(g, ["payments/app.py", "docs/guide.md", "orders/Dockerfile"])
    assert hit == {"payments": ["payments/app.py"], "orders": ["orders/Dockerfile"]} and unowned == ["docs/guide.md"]


def test_route_with_callers_is_risky_with_cited_chain(repo):
    root, g = repo
    files, diff = change(root, "payments/app.py", FILES["payments/app.py"].replace("'/pay'", "'/v2/pay'"))
    f = pr_review.facts(g, files, diff)
    v, why = pr_review.verdict(f)
    # the old route is gone; orders still calls it: broken by this PR
    assert v == "risky" and [(x["caller"], x["route"], x["new"]) for x in f["broken"]] == [("orders", "POST /pay", True)]
    assert any("does not exist after this change" in w for w in why)
    assert [a["service"] for a in f["services"][0]["affected"]] == ["orders", "web"]   # multi-hop, nearest first
    ids = {c["id"] for c in f["connections"]}
    assert all(c in ids for a in f["services"][0]["affected"] for c in a["cites"])      # every hop is a cited fact
    comment, res = pr_review.review_change(g, files, diff, "https://github.com/o/r", "abc", use_ai=False)
    assert comment.startswith(pr_review.MARKER) and "🔴 Risky" in comment and "```mermaid" in comment
    assert "https://github.com/o/r/blob/abc/orders/app.py#L" in comment and res["verdict"] == "risky"


def test_changed_route_that_keeps_its_path_lists_its_callers(repo):
    root, g = repo
    files, diff = change(root, "payments/app.py", FILES["payments/app.py"].replace("'ok'", "{'status': 'ok'}"))
    f = pr_review.facts(g, files, diff)
    assert not f["broken"] and f["routes"][0]["callers"][0]["caller"] == "orders"
    assert pr_review.verdict(f)[0] == "risky"


def test_older_mismatch_is_review_not_blamed_on_this_pr(repo):
    root, g = repo
    web = FILES["web/app.py"] + "\ndef old():\n    return requests.post('http://orders:5000/legacy-orders')\n"
    (root / "web/app.py").write_text(web)
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "older bug")
    mapper.map_repo(root, "test_pr_review")
    files, diff = change(root, "orders/app.py", FILES["orders/app.py"] + "# comment only\n")
    f = pr_review.facts(g, files, diff)
    assert [(x["caller"], x["route"], x["new"]) for x in f["broken"]] == [("web", "POST /legacy-orders", False)]
    v, why = pr_review.verdict(f)
    assert any("does not declare" in w for w in why) and not any("after this change" in w for w in why)


def test_leaf_docs_and_past_incident_verdicts(repo):
    root, g = repo
    files, _ = change(root, "audit/app.py", "def log(x):\n    print('audit', x)\n")
    assert pr_review.verdict(pr_review.facts(g, files))[0] == "safe"                    # nothing depends on audit
    files, _ = change(root, "docs/guide.md", "# guide v2\n")
    f = pr_review.facts(g, files)
    assert pr_review.verdict(f)[0] == "safe" and f["unowned_files"] == ["docs/guide.md"] and not f["services"]
    files, _ = change(root, "web/app.py", FILES["web/app.py"] + "# tweak\n")
    assert pr_review.verdict(pr_review.facts(g, files))[0] == "safe"                    # web is an entry point
    add_incident(g, "payments")
    f = pr_review.facts(g, ["payments/app.py"])
    v, why = pr_review.verdict(f)
    assert v == "risky" and f["incidents"][0]["incident"] == "INC-7" and any("INC-7" in w for w in why)


def fake_ai(review):
    return lambda prompt, key, model, **kw: (review, {"totalTokenCount": 1234})


def test_ai_findings_must_cite_facts_and_name_only_known_services(repo):
    root, g = repo
    files, diff = change(root, "orders/app.py", FILES["orders/app.py"].replace("/pay'", "/charge'"))
    f = pr_review.facts(g, files, diff)
    rule_v, _ = pr_review.verdict(f)
    # orders now calls POST /charge, which payments doesn't have: a new broken call made by this PR
    assert [(x["caller"], x["service"], x["route"], x["new"]) for x in f["broken"]] == [("orders", "payments", "POST /charge", True)]
    assert rule_v == "risky"
    c = f["connections"][0]["id"]
    ai = {"verdict": "safe", "summary": "orders now calls a different payments route.", "findings": [
        {"severity": "high", "title": "payments route renamed", "detail": "web reaches this via orders.", "cites": [c, "S1"]},
        {"severity": "medium", "title": "uncited", "detail": "trust me", "cites": ["C99"]},
        {"severity": "low", "title": "invented", "detail": "audit will break too", "cites": [c]},   # audit not affected
    ], "reviewer_checklist": ["Is /charge deployed in payments first?", "Ask audit owners"]}
    review, report = pr_review.ai_review(g, f, diff, rule_v, key="k", call=fake_ai(ai))
    assert [x["title"] for x in review["findings"]] == ["payments route renamed"]
    assert {d["title"] for d in report["dropped"]} == {"uncited", "invented"}
    assert review["checklist"] == ["Is /charge deployed in payments first?"]
    assert review["verdict"] == rule_v and review["ai_verdict"] == "safe"                 # rules are the floor
    comment = pr_review.render(f, rule_v, ["r"], review, report)
    assert "payments route renamed" in comment and "2 dropped" in comment and "rules set the floor" in comment


def test_ai_can_raise_but_bad_summary_is_removed(repo):
    _, g = repo
    f = pr_review.facts(g, ["audit/app.py"])
    assert not f["services"][0]["affected"]
    review, _ = pr_review.check_ai(g, f, {"verdict": "review", "summary": "web depends on this", "findings": []}, "safe")
    assert review["verdict"] == "review" and review["summary"] == ""


def test_ai_off_or_failing_falls_back_to_rules(repo, monkeypatch):
    _, g = repo
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    f = pr_review.facts(g, ["payments/app.py"])
    assert pr_review.ai_review(g, f, "", "risky")[0] is None

    def boom(*a, **k):
        raise RuntimeError("daily Gemini budget spent (20 requests in 24 h)")
    review, why = pr_review.ai_review(g, f, "", "risky", key="k", call=boom)
    assert review is None and "budget" in why
    comment, res = pr_review.review_change(g, ["payments/app.py"], "", use_ai=True)
    assert "Rule-based review" in comment and res["verdict"] == "risky"


def test_rate_gate_minute_and_day(tmp_path):
    ledger, slept, clock = tmp_path / "calls.json", [], [1000.0]
    gate = lambda rpm, rpd: pr_review._rate_gate(rpm, rpd, ledger, sleep=slept.append, now=lambda: clock[0])
    for _ in range(5):
        gate(5, 20)
    assert not slept
    gate(5, 20)                                   # 6th within a minute waits for the oldest to age out
    assert slept and 55 <= slept[0] <= 61
    pr_review._rate_gate(0, 0, tmp_path / "none.json")      # no cap configured: no ledger, no waiting
    assert not (tmp_path / "none.json").exists()
    ledger.write_text(json.dumps([clock[0] - 10 * i for i in range(20)]))
    with pytest.raises(RuntimeError, match="daily"):
        gate(5, 20)
    ledger.write_text(json.dumps([clock[0] - 90000] * 20))   # older than a day: budget is back
    gate(5, 20)


def test_call_gemini_parses_and_retries(monkeypatch):
    monkeypatch.setattr(pr_review.time, "sleep", lambda s: None)
    ok = {"candidates": [{"content": {"parts": [{"text": '{"verdict": "review", "summary": "s", "findings": []}'}]}}],
          "usageMetadata": {"totalTokenCount": 9}}
    calls = []

    def post(model, key, body):
        calls.append(body)
        if len(calls) == 1:
            raise urllib.error.HTTPError("u", 429, "busy", {}, __import__("io").BytesIO(b'{"retryDelay": "2s"}'))
        return ok
    out, usage = pr_review.call_gemini("p", "k", "gemini-flash-latest", post=post, gate=lambda *a: None)
    assert out["verdict"] == "review" and usage["totalTokenCount"] == 9 and len(calls) == 2
    assert calls[0]["generationConfig"]["responseSchema"]["properties"]["verdict"]["enum"] == ["safe", "review", "risky"]

    def bad(model, key, body):
        raise urllib.error.HTTPError("u", 400, "bad", {}, __import__("io").BytesIO(b"API key not valid"))
    with pytest.raises(RuntimeError, match="HTTP 400"):
        pr_review.call_gemini("p", "k", "m", post=bad, gate=lambda *a: None)


def test_cli_end_to_end(repo, tmp_path, monkeypatch):
    root, _ = repo
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    change(root, "payments/app.py", FILES["payments/app.py"].replace("'ok'", "'paid'"))
    out = tmp_path / "comment.md"
    pr_review.main(["--repo", str(root), "--base", "main", "--head", "pr", "--graph", GRAPH, "--map", "--out", str(out),
                    "--repo-url", "https://github.com/o/r"])
    text = out.read_text()
    assert "🔴 Risky" in text and "`payments`" in text and "/blob/" in text
