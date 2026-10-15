# NextTrace: project plan

> When something breaks, NextTrace shows which service failed first, who it hurts, and what fixed it last time.

Companion files: [instructions.md](instructions.md) (event rules, schedule, checklist) and [graph-db-explained.html](graph-db-explained.html) (graph DB primer).
Building starts Oct 16 morning; submit by **Oct 18, 6 PM IST**.

---

## 1. What it does

### In plain words
A website is many small programs ("services") calling each other. When one fails, everything that depends on it starts failing too, so the on-call engineer gets dozens of alarms at once and has to guess which one is the real cause.

NextTrace:
1. keeps a **map** of which service calls which (a graph in FalkorDB),
2. pins every alarm onto that map,
3. walks the map to find the service that failed **first** (the one that isn't just a victim of something below it),
4. shows the **path** of how the failure spread and **who is affected** ("customers can't check out"),
5. **remembers** every incident and what fixed it, so the next similar outage is solved faster,
6. does this with **several agents** that share one memory and hand work to each other safely.

### Who suffers today
On-call engineers and SREs. They are woken at night, flooded with alerts, and rely on what colleagues remember from past outages. When that knowledge isn't written down, the same diagnosis gets redone every time.

### What it is not
- Not a monitoring tool. We take alarms as input; we don't collect metrics from live systems.
- Not auto-remediation. It recommends and pages; a human decides.
- Not a novelty claim. AI SRE products exist. Our claim is narrower and **measured**: the graph makes the agent more accurate, explainable and faster on a public benchmark, and memory and coordination work correctly.

---

## 2. Architecture

```
 RCAEval case (metrics CSV)          Online Boutique topology (hand-encoded)
            │                                      │
            ▼                                      ▼
   detect.py: anomaly → Signals ────────►  FalkorDB  (one graph per tenant: acme, globex)
                                              ▲   ▲   ▲
             ┌────────────────────────────────┘   │   └──────────────────────┐
             │ MERGE signals (parallel)           │ Cypher tools            │ write resolution,
      Triage agents ──handoff──►  Investigator agent  ──handoff──►  Scribe agent    runbook stats
                                           │
                                           ▼
                         FastAPI  ──►  Web UI (graph, path highlight, agent trace,
                                       graph-off toggle, "what if?", "why?")
                                           │
                                     MCP server (stretch): same tools for Claude Code / Cursor
```

**Stack (keep it boring):**
- Python 3.12
- `falkordb` client
- FastAPI
- a single static HTML page with a JS graph library (Cytoscape.js or vis-network, whichever renders the path highlight fastest)
- FalkorDB in Docker locally; FalkorDB Cloud for the deployed version
- LLM: one Claude model for **all** LLM setups (same model, temperature 0), so comparisons are fair. Default `claude-sonnet-5-5`.

**Repo layout:**
```
nexttrace/
  topology/online_boutique.cypher   # services + CALLS edges + owners + user_function
  ingest/detect.py                  # metrics → anomalous signals
  ingest/load_case.py               # case → Incident + Signals in the graph
  graph/queries.py                  # EVERY Cypher query lives here (README links to it)
  agents/triage.py investigator.py scribe.py
  bench/run.py score.py results/    # benchmark runner, scorer, saved results (committed)
  api/server.py
  web/index.html
  tests/                            # concurrency, isolation, idempotency, scorer
data/                               # raw RCAEval downloads (gitignored)
labels/                             # ground truth (scorer only; never loaded into the graph)
```

---

## 3. Data

### 3.1 Failure cases: RCAEval
- **Source:** https://zenodo.org/records/14590730. Paper: https://arxiv.org/abs/2412.17015
- **License:** CC BY 4.0. Credit it in the README.
- **Use:** `RE1-OB.zip` (31 MB). 125 failure cases on Online Boutique, fault types CPU / MEM / DISK / DELAY / LOSS, metrics only, each labeled with the root-cause service.
- **Stretch:** `RE1-TT.zip` (280 MB, Train Ticket, much bigger topology). Do not touch RE2 (1.2–2.8 GB, logs + traces); there's no time.
- **Also available:** `pip install RCAEval` ships loaders and 15 baseline methods. Use the loaders if they save time.

**Verify in hour 1 (unknowns):**
- [ ] the file layout of one case (metrics CSV columns, units, sampling rate)
- [ ] whether each case includes the **fault injection time** (needed to split before vs after)
- [ ] the exact service names used in metric columns (they must match our topology node names)
- [ ] where the ground-truth label lives (folder name or a file)

### 3.2 Topology: Online Boutique
Hand-encode from Google's public repo: https://github.com/GoogleCloudPlatform/microservices-demo (architecture diagram in the README).
Expected edges, **verify against the diagram**:
```
frontend → adservice, recommendationservice, productcatalogservice, cartservice,
           shippingservice, currencyservice, checkoutservice
checkoutservice → productcatalogservice, cartservice, shippingservice,
                  currencyservice, paymentservice, emailservice
recommendationservice → productcatalogservice
cartservice → redis-cart
```
Add synthetic properties we own:
- `owner_team`, e.g. "payments-team"
- `tier`: 1 = critical
- `user_function`: "checkout", "browse products", "see recommendations"

These drive paging and the business-impact view.

### 3.3 Anonymization (mandatory for the benchmark)
Map every service to `svc-a`, `svc-b`, ... with a fixed, random mapping (seeded and saved). Online Boutique is famous, so an LLM may already "know" that frontend calls checkout. Anonymizing forces every setup to use only the data we give it. The UI can show real names; benchmarks always use anonymized ones.

### 3.4 Fallback: synthetic faults
If RCAEval cases can't be turned into clean signals by end of Day 1:
1. Pick a root service.
2. Mark it anomalous.
3. Propagate anomalies upstream along `CALLS` with a probability per hop.
4. Add random noise alarms elsewhere.

Ground truth is known by construction. This is weaker evidence (we made the data), so say so in the README.

### 3.5 Data rules
Everything is public (RCAEval, the microservices-demo topology) or synthetic (owners, runbooks, tenants). No personal data. `data/` is gitignored; the README tells people how to download it.

---

## 4. From metrics to signals (`detect.py`)

Keep it simple and identical for every setup:
1. For each metric column: baseline = the window **before** injection; test = the window **after**.
2. z = (max deviation in the test window − baseline mean) / baseline std. Guard against std = 0.
3. A metric is anomalous if |z| ≥ 3 (tunable; pick it once on a small dev split and freeze it).
4. A **Signal** = (service, metric, z, onset time). Onset = first timestamp past the threshold.

Write a tiny assert-based self-check: a synthetic series with a planted spike must produce exactly one signal.

> Risk: the detector, not the graph, may be the weak link. Mitigation: every setup gets the same signals, so differences come from reasoning, not detection.

---

## 5. Graph model

One FalkorDB graph per tenant (`acme`, `globex`; `bench` for benchmark runs).

```
(:Service {name, owner_team, tier, user_function})
(:Service)-[:CALLS]->(:Service)
(:Signal {key, metric, zscore, onset})-[:ON]->(:Service)      key = service:metric:window
(:Incident {id, status, version, owner_agent, fault_type, opened_at})
(:Incident)-[:HAS_SIGNAL]->(:Signal)
(:Incident)-[:SUSPECTS {rank, score}]->(:Service)
(:Incident)-[:CAUSED_BY]->(:Service)                          confirmed root cause (episodic)
(:Runbook {fault_type, steps, attempts, successes})           what to do (procedural)
(:Incident)-[:APPLIED {worked}]->(:Runbook)
(:Agent {name})-[:WROTE {ts, action}]->(:Incident)            provenance + handoff log
```

Memory types (Track 2 vocabulary):
- **Semantic:** the topology, owners and user functions. Facts about the world.
- **Episodic:** past incidents with their signals and confirmed causes.
- **Procedural:** runbooks with attempt and success counts that update over time.
- **Conversation history:** the agent's tool calls and conclusions per incident, stored via `WROTE`.

Incident status machine: `open → triaging → investigating → mitigating → resolved`. Only the owning agent may advance it, via compare-and-swap.

---

## 6. Queries (all in `graph/queries.py`)

**Syntax caveats:**
- Run each query in FalkorDB Browser on Day 1 before wiring it into an agent.
- `shortestPath` needs both endpoints matched first, and no property filters inside the pattern.
- For the exact `algo.*` arguments, see https://docs.falkordb.com/algorithms

| Tool | Purpose | Sketch |
|---|---|---|
| `rca_candidates` | culprit ranking | anomalous services with fewest anomalous downstream deps, then most anomalous upstream callers explained |
| `blast_radius` | who is hurt | `MATCH (up:Service)-[:CALLS*1..6]->(:Service {name:$svc}) RETURN DISTINCT up.name, up.owner_team, up.user_function` |
| `explain_path` | the "why" | `MATCH (a:Service {name:'frontend'}),(b:Service {name:$rc}) WITH shortestPath((a)-[:CALLS*]->(b)) AS p RETURN [n IN nodes(p) | n.name]` |
| `similar_incidents` | episodic recall | Jaccard overlap of signal keys with resolved incidents; returns cause + runbook that worked |
| `handoff` | safe coordination | `MATCH (i:Incident {id:$id}) WHERE i.version=$v AND i.status=$from SET i.status=$to, i.version=$v+1, i.owner_agent=$agent RETURN i.version` (0 rows = lost the race, re-read and retry) |
| `ingest_signal` | idempotent writes | `MERGE (s:Signal {key:$key}) ... MERGE (i)-[:HAS_SIGNAL]->(s)` |
| `what_if` | F1 button | same as `blast_radius`, called from the UI |

`rca_candidates` full sketch:
```cypher
MATCH (i:Incident {id:$id})-[:HAS_SIGNAL]->(:Signal)-[:ON]->(s:Service)
WITH collect(DISTINCT s) AS bad
UNWIND bad AS s
OPTIONAL MATCH (s)-[:CALLS*1..4]->(d:Service) WHERE d IN bad
WITH s, bad, count(DISTINCT d) AS badDeps
OPTIONAL MATCH (s)<-[:CALLS*1..4]-(u:Service) WHERE u IN bad
RETURN s.name AS service, badDeps, count(DISTINCT u) AS explains
ORDER BY badDeps ASC, explains DESC
LIMIT 3
```

---

## 7. Agents

| Agent | Job | Reads | Writes |
|---|---|---|---|
| **Triage** (N parallel workers) | turn raw alarms into signals, group them into one incident | topology | `Signal`, `Incident`, `HAS_SIGNAL` (via `MERGE`) |
| **Investigator** (LLM with tools) | find the culprit, the path and the impact; check memory | all tools | `SUSPECTS`, conclusion, `WROTE` |
| **Scribe** | record the outcome, update runbook stats, page the owner, write the incident report | incident, runbooks | `CAUSED_BY`, `APPLIED`, `Runbook` counts, mock webhook |

Handoffs go **only** through the graph, using the `handoff` CAS. No agent passes state to another in memory, which is what makes "shared state" real and testable.

Investigator tool loop: the model sees tool descriptions, picks tools, and every tool call and result is logged (shown in the UI trace and stored for "why?" links). Cap it at about 8 tool calls per incident.

---

## 8. Features

### Core (must ship)
| # | Feature | Acceptance check |
|---|---|---|
| C1 | Load a case → incident with signals in the graph | case ID in → nodes visible in FalkorDB Browser |
| C2 | Culprit + path + blast radius | UI shows top suspect, highlighted path, affected services |
| C3 | Graph-off toggle | same case, LLM-only answer shown side by side |
| C4 | Episodic + procedural memory | a resolved incident shows up in `similar_incidents` for a later similar case, even after a process restart |
| C5 | Three agents with CAS handoff | the handoff timeline shows version bumps; no double ownership |
| C6 | Per-tenant graphs | `acme` and `globex` separate; isolation test passes |
| C7 | Benchmark + results committed | `bench/results/*.json` + table in README |

### Extras (in order; only after the core works)
| # | Feature | What the user sees | Est. |
|---|---|---|---|
| F1 | "What if?" | click a service → everything that would break lights up | ~2h |
| F2 | "Why?" links | each claim links to its path + signals + tool call | ~3h |
| F3 | "I'm not sure" mode | if the top-2 scores are close or there are few signals: show 3 suspects with scores | ~1h |
| F4 | Business impact | "customers can't check out" from `user_function` | ~2h |
| F5 | Auto incident report | markdown postmortem generated from the graph | ~2h |
| F6 | MCP server | Claude Code / Cursor can call the same tools | ~3h |

Hours are estimates. **Cut order:** F6 → F5 → F4 → UI polish → live deploy → second tenant.

---

## 9. Testing with and without the graph (the removal test, measured)

This is the centrepiece of the submission. Run it early (Day 1 evening) and keep re-running it.

### 9.1 Setups
| ID | Name | Gets | Purpose |
|---|---|---|---|
| **A** | LLM only | anonymized signal list as text | "no graph at all" |
| **A+** | LLM + topology pasted as text | signal list + adjacency list in the prompt | the honest control: "why not just paste the graph into the prompt?" |
| **B** | Graph query only | `rca_candidates` top 3, no LLM | is the graph logic itself sound? |
| **C** | Graph agent | LLM with graph tools | the product |
| **C+mem** | Graph agent + memory | C plus memory built from the training split | does memory help? |

**Why A+ matters:** a judge may ask "couldn't you just put the map in the prompt?" On an 11-service system, that may work fine. Report it honestly. Our answer is that the graph still wins on things a prompt can't do:
- scale (Train Ticket stretch)
- exact paths
- persistent memory across sessions and agents
- safe concurrent writes
- per-tenant isolation

If A+ ≈ C on accuracy, lead with tokens, cost, latency and the other wins instead.

### 9.2 Fairness rules
- Same signals for every setup (same `detect.py` output, frozen).
- Anonymized names in every setup.
- Same LLM model, temperature 0, same answer format: a ranked list of 3 service IDs.
- Ground truth is never visible to any setup. The scorer reads `labels/` only.
- Fix the detector threshold on a small dev split (about 10 cases), then never touch it again. Report results on the rest.

### 9.3 Metrics
- **Acc@1:** the true root cause is ranked first.
- **Acc@3:** the true root cause is in the top 3.
- **Tokens and cost** per case (LLM setups).
- **Latency** per case.
- Breakdown **per fault type** (CPU / MEM / DISK / DELAY / LOSS).
- For context, cite RCAEval's own baseline numbers from the paper. Copy them exactly, don't paraphrase.

### 9.4 Procedure
```
python -m bench.run --setup A    --cases eval   # writes bench/results/A.json
python -m bench.run --setup A+   --cases eval
python -m bench.run --setup B    --cases eval
python -m bench.run --setup C    --cases eval
python -m bench.score bench/results/*.json      # prints the table for README
```
Save raw outputs per case (answer, tokens, time). Do an error analysis on 5 failures per setup, with one line each on why it was wrong.

### 9.5 Memory experiment (C vs C+mem)
1. Split the cases by a fixed seed: **train 60% / eval 40%**, stratified by fault type.
2. "Resolve" the train cases into the graph: incidents, confirmed cause, runbook outcome. (This simulates past outages that engineers confirmed.)
3. Run C on eval with memory **wiped** (cold) and with memory **loaded** (warm).
4. Compare Acc@1, tool calls, tokens and time.
5. **Never write eval-case labels into memory.** That would be cheating, and a judge will ask.
6. Restart demo: kill the process between train and eval, to show the memory lives in FalkorDB, not in the process.

### 9.6 Coordination tests (`tests/`, assert-based)
| Test | Setup | Pass condition |
|---|---|---|
| Idempotent ingest | 50 async writers push the same 500 alarms (with duplicates) | exactly 1 incident; signal count = unique keys |
| CAS handoff | 10 agents race to claim the same incident | exactly 1 winner; final version = number of successful transitions |
| No lost updates | concurrent runbook success increments | final count = number of increments |
| Isolation | write to `acme`, query `globex` | zero rows |
| Latency | the idempotent-ingest run | report p50/p99 write latency (no target; just report it) |

Docs say only one write query runs at a time per graph (FIFO queue), and each write query is atomic (see [BUILD_GUIDE.md](BUILD_GUIDE.md) §3.6). So expect writes to one graph to queue (p99 grows with writer count) while writes to different tenant graphs can proceed in parallel. The last part is inferred, so these tests confirm it. Report what happens; don't assume.

### 9.7 Results table template (README)
```
| Setup | Acc@1 | Acc@3 | Avg tokens | Avg latency |
|-------|-------|-------|------------|-------------|
| A  LLM only            |   |   |   |   |
| A+ LLM + topology text |   |   |   |   |
| B  Graph query only    |   |   | 0 |   |
| C  Graph agent         |   |   |   |   |
| C+mem (warm memory)    |   |   |   |   |
n = ?, dataset RCAEval RE1-OB (CC BY 4.0), model = ?, date = ?
```
Fill it only with real runs. If a number is bad, keep it and explain it.

---

## 10. What the judges want, and where we show it

No rubric is published. This mapping uses the official track focus areas.

| Their words | Where NextTrace shows it |
|---|---|
| "Multi-hop reasoning" | `rca_candidates`, `blast_radius` (`CALLS*1..6`) |
| "Graph queries and algorithms as agent tools" | investigator tool loop; shortestPath; optional PageRank/betweenness for criticality |
| "Tool selection through MCP" | F6 MCP server (stretch) |
| "Actions based on what the graph shows" | paging the owner team from `owner_team`; runbook suggestion |
| "Explainable results" | highlighted path + "why?" links + stored tool trace |
| "Episodic, semantic, and procedural memory" | incidents / topology / runbooks (section 5) |
| "Persistent conversation history" | `WROTE` log per incident, survives restart |
| "Shared state and handoffs between agents" | CAS status machine, three agents |
| "High-concurrency reads and writes" | 9.6 tests + latency numbers |
| "Separate memory per user or tenant" | one graph per tenant + isolation test |
| Removal test ("graph is decoration") | section 9: measured, with the A+ control |
| "Show the path behind an answer" | `explain_path` lit up in the UI |

Also required in the submission (see instructions.md): a README with the graph model + every Cypher query, demo video, deploy or local setup, tracks named, AI disclosure.

---

## 11. Demo (3 minutes)

1. **Hook (0:00):** "3am. 40 alerts. Which one is real?"
2. **Find it (0:20):** pick a case with the answer hidden → alarms stream in → path lights up → culprit, impact, owner paged → click "why?" → reveal the ground truth.
3. **Prove it (1:15):** toggle graph off on the same case → show the results table (A / A+ / B / C).
4. **Remember it (1:45):** restart → new case of the same type → "matches INC-…, runbook worked 2/2" → fewer tool calls on screen.
5. **Quick hits (2:15):** "what if?" button; 50 writers → 1 incident; tenant switch.
6. **Close (2:45):** architecture + one Cypher query.

Record several takes on Oct 18 morning. Use replay mode (cached runs) if live LLM calls are slow.

---

## 12. Timeline

| Day | Morning | Afternoon | Evening | Exit criterion |
|---|---|---|---|---|
| **Oct 16** | Docker FalkorDB; download RE1-OB; answer the hour-1 unknowns (3.1); topology + anonymization | `detect.py` + self-check; `load_case.py`; `rca_candidates`; setup B on all cases | setups A and A+; **Day 1 checkpoint**; post organizer questions | Acc@1 for A, A+, B |
| **Oct 17** | Tools; investigator loop; setup C | Triage + Scribe; CAS handoff; memory writes; memory experiment | 9.6 tests; minimal UI (case picker, path, trace, toggle); F1, F3 | Full results table + green tests + clickable demo |
| **Oct 18** | F2; deploy with replay mode; README | Fresh-clone test; video; blog post | **Submit by 6 PM IST** | Submission confirmed |

**Day 1 checkpoint decisions:**
- **C/B clearly beat A and A+:** keep the "graph is smarter" story.
- **Close to A+:** lead with tokens, latency, explanations, memory and coordination; try RE1-TT if there's time.
- **Signals unusable:** switch to the synthetic fallback (3.4). Switch to backup idea H1 only if that also fails.

---

## 13. Risks

| Risk | Likelihood (judgment) | Mitigation |
|---|---|---|
| A+ matches C on a small topology | medium | report it; lean on the other wins; Train Ticket stretch |
| Weak anomaly detector drags every setup down | medium | same signals for all; tune once on the dev split |
| RCAEval format surprises eat Day 1 | medium | use the RCAEval loaders; synthetic fallback |
| Cypher syntax differences in FalkorDB | low–medium | test each query in Browser first |
| UI eats time | high | minimal UI; FalkorDB Browser as fallback |
| LLM cost/latency on the public deploy | medium | replay mode; rate limit |
| Judges see "yet another AI SRE" | medium | lead with the measured removal test, not novelty |

---

## 14. Definition of done

- [ ] All setups in 9.1 run end to end and the results are committed
- [ ] All tests in 9.6 pass
- [ ] Demo shows all three beats from a fresh start
- [ ] README: pitch, architecture, graph model, every query, results table, data credits, setup, AI disclosure
- [ ] Video uploaded, deploy works in incognito, submission confirmed
