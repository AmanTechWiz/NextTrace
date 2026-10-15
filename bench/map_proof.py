"""Map proof: compare NextTrace's map of a real repo with calls seen in recorded real traffic (RCAEval RE2 traces).

  uv run --with remotezip python -m bench.map_proof --fetch    # download trace files once (range requests)
  uv run python -m bench.map_proof                              # compare and write docs/map_report.md

Ground truth is the traffic, which we did not write. The repo map is built without looking at it.
"""
import argparse
import collections
import json
import random
import re
import subprocess
from pathlib import Path

import pandas as pd

from nexttrace import endpoints as ep
from nexttrace import mapper

ROOT = Path(__file__).parent.parent
ZIP = "https://zenodo.org/records/14590730/files/{}.zip?download=1"
SYSTEMS = {  # name -> (local repo, RE2 zip, trace files to use)
    "Train Ticket": (ROOT / "data/repos/train-ticket", "RE2-TT", 10),
    "Online Boutique": (ROOT / "data/repos/boutique", "RE2-OB", 5),
}
COLS = ["spanID", "parentSpanID", "serviceName", "operationName"]
MAX_MB = 80
GOOGLE_DIAGRAM = {("frontend", s) for s in ("adservice", "recommendationservice", "productcatalogservice", "cartservice",
                                             "shippingservice", "currencyservice", "checkoutservice")} | \
                 {("checkoutservice", s) for s in ("productcatalogservice", "cartservice", "shippingservice",
                                                   "currencyservice", "paymentservice", "emailservice")} | \
                 {("recommendationservice", "productcatalogservice"), ("cartservice", "redis-cart"), ("loadgenerator", "frontend")}


def trace_dir(zip_name):
    return ROOT / "data" / "traces" / zip_name


def fetch(zip_name, n):
    """Download n trace files (<= MAX_MB, spread across fault types), keeping only the columns we need."""
    from remotezip import RemoteZip
    out = trace_dir(zip_name)
    out.mkdir(parents=True, exist_ok=True)
    with RemoteZip(ZIP.format(zip_name)) as z:
        files = [i for i in z.infolist() if i.filename.endswith("/traces.csv") and 1e5 < i.file_size <= MAX_MB * 1e6]
        by_fault = collections.defaultdict(list)
        for i in sorted(files, key=lambda i: i.filename):
            by_fault[i.filename.split("/")[1].rsplit("_", 1)[1]].append(i)
        picked = []
        while len(picked) < n and any(by_fault.values()):  # round-robin over fault types
            for fault in sorted(by_fault):
                if by_fault[fault] and len(picked) < n:
                    picked.append(by_fault[fault].pop(0))
        for i in picked:
            name = "_".join(i.filename.split("/")[1:3]) + ".csv"
            if not (out / name).exists():
                with z.open(i.filename) as f:
                    pd.read_csv(f, usecols=COLS).to_csv(out / name, index=False)
            print("  have", name)


def observed(files, known):
    """(caller, callee) -> count and (caller, callee, op) -> count from parent/child spans of different services."""
    def norm(s):  # traces may say "frontendservice" where the repo says "frontend"
        return s if s in known or s.removesuffix("service") not in known else s.removesuffix("service")
    svc_calls, ep_calls, active = collections.Counter(), collections.Counter(), set()
    for f in files:
        df = pd.read_csv(f, usecols=COLS)
        df["serviceName"] = df["serviceName"].map(norm)
        active |= set(df["serviceName"])
        parent = dict(zip(df["spanID"], df["serviceName"]))
        df["caller"] = df["parentSpanID"].map(parent)
        df = df[df["caller"].notna() & (df["caller"] != df["serviceName"])]
        svc_calls.update(zip(df["caller"], df["serviceName"]))
        for a, b, op in zip(df["caller"], df["serviceName"], df["operationName"]):
            m = re.match(r"^([A-Z]+) (/\S*)$", str(op))
            key = f"{b} {m.group(1)} {m.group(2)}" if m else (f"{b} RPC {op.split('.')[-1]}" if "/" in str(op) else None)
            if key:
                ep_calls[(a, key)] += 1
    return svc_calls, ep_calls, active


def gh_link(repo: Path, commit, where):
    url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    path, _, line = where.rpartition(":")
    return f"{url.removesuffix('.git')}/blob/{commit}/{path}#L{line}"


def analyze(name, repo, zip_name):
    files = sorted(trace_dir(zip_name).glob("*.csv"))
    if not files:
        raise SystemExit(f"no traces for {name}: run with --fetch first")
    m = mapper.scan(repo)
    routes, owner = ep.routes(repo, m)
    linked = ep.link(ep.calls(repo, m, owner), routes)
    svc_obs, ep_obs, active = observed(files, set(m.services))
    scanned = set(m.edges)
    kinds = {n: s["kind"] for n, s in m.services.items()}

    obs = set(svc_obs)
    undeclared = sorted(e for e in obs if e[0] not in m.services or e[1] not in m.services)
    comparable = obs - set(undeclared)
    # a scanned call can only be checked against traffic when both services were running and instrumented
    checkable = {e for e in scanned if e[0] in active and e[1] in active}
    unseen = sorted(checkable - obs)
    not_checkable = collections.Counter(
        "callee is a datastore or infra (not traced)" if kinds.get(b) in ("datastore", "infra")
        else "caller has no spans in these traces" if a not in active else "callee has no spans in these traces"
        for a, b in scanned - checkable)

    ours_ep = {(c.caller, r.key) for c, r in linked if r}
    ep_seen = set(ep_obs)
    first_ev = {e: sorted(evs, key=lambda x: (-mapper.RANK[x.source], x))[0] for e, evs in m.edges.items()}
    return {
        "system": name, "repo": m.repo, "commit": m.commit, "trace_files": [f.name for f in files],
        "spans_calls": sum(svc_obs.values()),
        "services": {"observed": len(obs), "found": len(comparable & scanned), "comparable": len(comparable),
                     "missed": sorted(map(list, comparable - scanned)), "undeclared": [list(e) for e in undeclared],
                     "scanned": len(scanned), "checkable": len(checkable), "seen_in_traffic": len(checkable & obs),
                     "unseen": [{"caller": a, "callee": b, "evidence": str(first_ev[(a, b)]),
                                 "link": gh_link(repo, m.commit, first_ev[(a, b)].where)} for a, b in unseen],
                     "not_checkable": dict(not_checkable)},
        "endpoints": {"observed": len(ep_seen), "found": len(ep_seen & ours_ep),
                      "missed": sorted(map(list, ep_seen - ours_ep)), "routes_parsed": len(routes),
                      "calls_mapped": len(linked), "calls_linked": sum(1 for _, r in linked if r),
                      "unmatched": [{"caller": c.caller, "callee": c.callee, "method": c.method, "path": c.path,
                                     "link": gh_link(repo, m.commit, c.where)} for c, r in linked if not r]},
        "diagram": None if "boutique" not in m.repo else
                   {"edges": len(GOOGLE_DIAGRAM), "found": len(GOOGLE_DIAGRAM & scanned)},
    }


def spot_check(results, n=10, seed=7):
    """A seeded random sample of unseen calls, for a human to read in the code (verdicts recorded in VERDICTS)."""
    pool = [(r["system"], u) for r in results for u in r["services"]["unseen"]]
    return random.Random(seed).sample(pool, min(n, len(pool)))


# Filled in by reading each sampled call site in the code (links in the report). Train Ticket v0.2.1, Oct 16 2026.
REAL = "real call in the code"
VERDICTS = {
    ("ts-admin-basic-info-service", "ts-contacts-service"): f"{REAL}: getAllContacts() GETs /contacts. Admin panel only.",
    ("ts-admin-basic-info-service", "ts-station-service"): f"{REAL}: `stations` URL used in 3 calls (lines 98, 114, 128). Admin panel only.",
    ("ts-admin-basic-info-service", "ts-train-service"): f"{REAL}: DELETE /trains/{{id}}. Admin panel only.",
    ("ts-preserve-service", "ts-consign-service"): f"{REAL}: createConsign() POSTs /consigns, only when a booking adds a consignment.",
    ("ts-preserve-other-service", "ts-consign-service"): f"{REAL}: same as preserve, for the other train type.",
    ("ts-user-service", "ts-auth-service"): f"{REAL}: AUTH_SERVICE_URI + \"/users/\" + userId (line 154). User-management action.",
}


def pct(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "n/a"


def render(results, sample):
    L = ["# Map proof: NextTrace's map vs recorded real traffic", "",
         "The map is built from the repo alone (config and code). The ground truth is recorded traffic from the public",
         "RCAEval RE2 dataset ([Zenodo](https://zenodo.org/records/14590730), CC BY 4.0), which we did not write.",
         "Regenerate: `uv run --with remotezip python -m bench.map_proof --fetch && uv run python -m bench.map_proof`.", ""]
    L += ["## Summary", "", "| System | Calls seen in traffic, found in the map | Endpoint calls seen in traffic, found | Google diagram |",
          "|---|---|---|---|"]
    for r in results:
        s, e, d = r["services"], r["endpoints"], r["diagram"]
        L.append(f"| {r['system']} | {pct(s['found'], s['comparable'])} | {pct(e['found'], e['observed'])} | "
                 f"{pct(d['found'], d['edges']) if d else '–'} |")
    for r in results:
        s, e = r["services"], r["endpoints"]
        L += ["", f"## {r['system']}", "",
              f"Repo `{r['repo']}` at commit `{r['commit'][:10]}`. Traffic: {len(r['trace_files'])} recorded runs, "
              f"{r['spans_calls']:,} cross-service calls.", "",
              "**Service level**", "",
              f"- Calls seen in traffic and found in the map: **{pct(s['found'], s['comparable'])}**.",
              f"- Missed: {s['missed'] or 'none'}.",
              f"- Seen in traffic between services the repo doesn't declare: {s['undeclared'] or 'none'}.",
              f"- The map has {s['scanned']} calls. {s['checkable']} can be checked against this traffic "
              f"(both services ran and were traced); {pct(s['seen_in_traffic'], s['checkable'])} of those were seen.",
              f"- Not checkable: " + (", ".join(f"{n} ({why})" for why, n in s["not_checkable"].items()) or "none") + ".",
              "", "**Endpoint level**", "",
              f"- Routes parsed: {e['routes_parsed']}. Call sites mapped: {e['calls_mapped']}, linked to a route: "
              f"{pct(e['calls_linked'], e['calls_mapped'])}.",
              f"- Endpoint calls seen in traffic and found in the map: **{pct(e['found'], e['observed'])}**.",
              f"- Missed: {e['missed'] or 'none'}."]
        if e["unmatched"]:
            L += ["- Calls to a route the callee does not declare (likely bugs):"]
            L += [f"  - `{u['caller']}` sends `{u['method'] or '?'} {u['path']}…` to `{u['callee']}` — [code]({u['link']})"
                  for u in e["unmatched"]]
        if s["unseen"]:
            L += ["", f"<details><summary>{len(s['unseen'])} mapped calls not seen in this traffic (each with its code line)</summary>", ""]
            L += [f"- `{u['caller']}` → `{u['callee']}` — [code]({u['link']})" for u in s["unseen"]]
            L += ["", "</details>"]
    total = sum(len(r["services"]["unseen"]) for r in results)
    real = sum(VERDICTS.get((u["caller"], u["callee"]), "").startswith(REAL) for _, u in sample)
    L += ["", "## Hand check of calls not seen in traffic", "",
          "Traffic only shows what the load generator exercised. "
          + (f"All {total}" if len(sample) == total else f"A seeded random sample of {len(sample)} of {total}")
          + f" mapped-but-unseen calls were read in the code: **{real}/{len(sample)} are real calls**.",
          "", "| System | Call | Verdict |", "|---|---|---|"]
    for system, u in sample:
        v = VERDICTS.get((u["caller"], u["callee"]), "not yet checked")
        L.append(f"| {system} | [`{u['caller']}` → `{u['callee']}`]({u['link']}) | {v} |")
    L += ["", "## Limits", "",
          "- Traffic covers only what the load generator did during these runs, so unseen ≠ wrong.",
          "- Databases and infra are not traced in this dataset, so calls to them are checked against config only.",
          "- Some services have no spans at all (e.g. Boutique's cart, shipping and ad services were not instrumented in",
          "  this dataset), so calls to or from them can't be checked here; Boutique's are covered by Google's diagram.",
          "- Train Ticket is mapped at release v0.2.1, the version whose service names match the dataset."]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="download the trace files first")
    a = ap.parse_args()
    if a.fetch:
        for name, (_, zip_name, n) in SYSTEMS.items():
            print("fetching", name)
            fetch(zip_name, n)
    results = [analyze(name, repo.resolve(), z) for name, (repo, z, _) in SYSTEMS.items()]
    sample = spot_check(results)
    out = ROOT / "bench" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / "map_proof.json").write_text(json.dumps({"results": results, "spot_check": sample}, indent=2) + "\n")
    (ROOT / "docs" / "map_report.md").write_text(render(results, sample))
    for r in results:
        print(f"{r['system']}: services {pct(r['services']['found'], r['services']['comparable'])} | "
              f"endpoints {pct(r['endpoints']['found'], r['endpoints']['observed'])} | "
              f"unseen {len(r['services']['unseen'])} | undeclared {r['services']['undeclared']}")


if __name__ == "__main__":
    main()
