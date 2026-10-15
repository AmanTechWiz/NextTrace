# RCAEval RE1-OB: what's in the data (inspected Oct 15, 2026)

Location: `data/RE1-OB/` (gitignored). Source: https://zenodo.org/records/14590730 (CC BY 4.0).
`data/raw/RE1-OB.zip` is a corrupt partial download; ignore it, the extracted folder is complete (250 files).

## Layout
`data/RE1-OB/<root_cause_service>_<fault>/<case 1-5>/{data.csv, inject_time.txt}`
- **Label = the folder name.** e.g. `currencyservice_disk/3` → root cause `currencyservice`, fault `disk`.
- **Injection time = `inject_time.txt`** (unix epoch seconds). It is inside the CSV's `time` range.
- 5 services x 5 faults x 5 cases = **125 cases**.
- Root-cause services that occur: adservice, cartservice, checkoutservice, currencyservice, productcatalogservice. Only 5; the other services (frontend, payment, email, recommendation, shipping, redis) are never the answer.
- Faults: cpu, mem, disk, delay, loss.

## CSV format
- 1 row per second; first column `time` (epoch seconds). No missing values.
- **delay / disk / loss:** 721 rows, injection at row 360 (6 min before, 6 min after), 56 metric columns.
- **cpu / mem:** 4201 rows, injection at row 2100 (35 min before, 35 min after). Exception: `checkoutservice_cpu/4` has 2827 rows (inject still at 2100).
- **cpu / mem files contain a duplicate column `time.1`: drop it.**
- Column naming differs by fault family: cpu/mem use `*_load` and `*_latency`; delay/disk/loss use `*_workload`, `*_latency-50`, `*_latency-90`. 15 distinct column sets in total, so the loader must normalize names (metric kind = text after the first `_`).
- Metric kinds: cpu, mem, workload/load, error, latency(-50/-90). Services in columns: adservice, cartservice, checkoutservice, currencyservice, emailservice, frontend, frontend-external (workload only), main, paymentservice, productcatalogservice, recommendationservice, redis, shippingservice. `main` and `frontend-external` are not in the Online Boutique call graph; decide in S3 (probably drop).
- There are no `disk` metrics even for disk faults; disk faults show up as cpu/mem/latency changes.

## A trivial baseline already scores high (important)
Rule: per service take the max |post-mean - pre-mean| / pre-std across its metrics; the biggest wins. No graph, no AI.
- Acc@1 **104/125 = 83%**, Acc@3 **115/125 = 92%**.
- By fault Acc@1: cpu 24/25, mem 25/25, disk 23/25, delay 23/25, **loss 9/25**.
- So the graph + agent must beat 83%; the headroom is the 21 misses, mostly `loss` faults.
- In the misses the pick is often a service that calls the true one (checkout, frontend), which is the "failure propagates upstream" pattern. Not all: `adservice_loss` picks `checkoutservice`, which does not call adservice in Online Boutique (to verify against the real topology). Do not claim the graph fixes these until S5 shows it.
- Caveat: this baseline compares post-mean vs pre-mean over the whole window and sees injection time. Our detector must do the same for every setup so comparisons stay fair.

## Consequences for the plan
1. Benchmarks must report this simple baseline as a fourth row (call it setup **D: no graph, no LLM**), next to A / A+ / B / C.
2. With only 5 possible answers, the memory experiment is easier than it looks: report cold vs warm honestly and don't oversell it.
3. Loader: drop `time.*`, normalize metric names, split at `inject_time.txt`, handle 721 / 4201 / 2827 row lengths.
4. Replay feeder: 1 row = 1 second, so 720 s (or 4200 s) per case; replay speed must be configurable.

## S3 exploratory check: does the map fix the misses? (Oct 15)
Map: Online Boutique, 11 services, 15 CALLS edges, from the manifests' `*_ADDR` settings (matches Google's diagram). `main` and `frontend-external` dropped (not services in the map).
Same per-service z-scores as the naive baseline; "anomalous" = z >= frac x the case's max z. Rules tried (all on all 125 cases, so **exploratory, not the benchmark**):

| Rule | frac | Acc@1 | fixed / broke vs naive | loss |
|---|---|---|---|---|
| naive: biggest z (no graph) | – | 104 | – | 9 |
| deepest anomalous (fewest anomalous deps), then z | 0.05–0.5 | 102–106 | 4–7 / 2–9 | 10–11 |
| most anomalous callers explained, then z | 0.1 / 0.3 | 103 / 107 | 7/8, 5/2 | 11 |
| z x (1+explains)/(1+badDeps) | 0.1 / 0.3 | **108** / 106 | 7/3, 4/2 | 12 / 10 |

**Takeaway:** on this 11-service dataset the map adds at most about +4 cases of accuracy (83% → 86%), and that best number was picked after seeing results. Leaf services always have zero anomalous dependencies, so "deepest anomalous" mostly picks the noisiest leaf. **Accuracy alone is not a strong story here.** Day 1 checkpoint is effectively decided early: lead with what the graph gives that a flat rule can't (the path, blast radius and owners, memory across sessions, safe multi-agent coordination, per-tenant isolation) and report accuracy honestly as "on par or slightly better". Optional upside: RE1-TT (Train Ticket, larger map) may show a bigger gap; that's a hypothesis, not a result.
