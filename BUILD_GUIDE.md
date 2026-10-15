# NextTrace: build guide (read this when you feel lost)

How the files fit together:
- [instructions.md](instructions.md): event rules, deadline, submission checklist
- [project_plan.md](project_plan.md): what we build and why, benchmarks, features
- **This file:** what the FalkorDB docs actually confirm, and the exact order to build in
- [graph-db-explained.html](graph-db-explained.html): primer

Facts below were read from docs.falkordb.com on Oct 15, 2026, then **tested on our machine the same day** (`tests/test_smoke.py`, 12/12 passing). Tags:
**[doc]** stated in the docs · **[tested]** we ran it and it works · **[inferred]** follows from the docs but isn't stated · **[unknown]** not yet tested.

**Build started Oct 15, 2:10 PM IST** (half a day earlier than planned). Steps S0 and S1 are done.

---

## 1. Your machine today (checked)

| Item | Status | Action |
|---|---|---|
| Docker via **OrbStack** (not Docker Desktop) | running | `open -a OrbStack` after a reboot |
| Python | system 3.14.6; **project uses 3.12.13 via uv** | `uv run ...` for everything |
| Node 22.16 / npm 10.9 | installed | not used (stack is Python) |
| git 2.50 | installed | |
| redis-cli | not on the Mac | use `docker exec falkordb redis-cli ...` or the Browser |
| `ANTHROPIC_API_KEY` | **not set** in this shell | export it before running any LLM code; never commit it |

## 2. Language decision: **Python + uv** (decided Oct 15; comparison kept for reference)

| | Python | TypeScript |
|---|---|---|
| FalkorDB client | `pip install falkordb` [doc] | `npm install falkordb` [doc] |
| Parameters | `graph.query(q, {'name': 'x'})` [doc] | `graph.query(q, { params: { name: 'x' } })` [doc, from README] |
| Read-only | `ro_query` [doc] | `roQuery` [doc] |
| Concurrent writers (50 parallel) | async client: `from falkordb.asyncio import FalkorDB` + redis `BlockingConnectionPool` [doc] | native `Promise.all` |
| Delete a graph | `g.delete()` [doc] | not documented, test it |
| CSV / z-scores | pandas | hand-rolled, about 1 extra hour |
| RCAEval loaders | `pip install RCAEval` | none (data is CSV in zips) |

**Rule:** pick the one you can debug fastest at 2 AM. If unsure, pick the one you've shipped more in.

---

## 3. Verified FalkorDB cheat sheet

### 3.1 Run it
```bash
docker volume create falkordb_data
docker run -d --name falkordb -v falkordb_data:/var/lib/falkordb/data -p 6379:6379 -p 3000:3000 \
  -e REDIS_ARGS="--appendonly yes --appendfsync everysec" falkordb/falkordb:latest
# Browser UI: http://localhost:3000   (this is the exact command running now)
```
- **[doc]** Image `falkordb/falkordb` includes the Browser (port 3000). `falkordb/falkordb-server` is the lighter image without it.
- **[doc]** The data directory in the container is `/var/lib/falkordb/data`; mounting a named volume there is what keeps data across restarts.
- **[doc]** Config goes in `-e FALKORDB_ARGS="THREAD_COUNT 4"`; Redis settings in `-e REDIS_ARGS="--requirepass x"`.
- **[tested]** Image defaults: `appendonly no`, snapshot `save 3600 1 300 100 60 10000`. `docker stop` kept data; **`docker kill` lost the last write.** With `REDIS_ARGS="--appendonly yes --appendfsync everysec"` a write survived `docker kill`.
- **[tested] Gotcha:** switching AOF on for an existing volume made Redis ignore the old snapshot (old test data vanished). Set persistence **before** loading real data, and never toggle it mid-build.
- **[tested]** Image ships with module args `MAX_QUEUED_QUERIES 25`, `TIMEOUT 1000` (deprecated param), `RESULTSET_SIZE 10000`; `THREAD_COUNT 10`, `TIMEOUT_MAX 0`, `TIMEOUT_DEFAULT 0`. 50 parallel writers were **not** rejected. `RESULTSET_SIZE 10000` caps rows returned per query, so keep that in mind for bulk reads.
- **[tested]** FalkorDB module version 42201 on Redis 8.10.2. Python client `falkordb` 1.8.0.

### 3.2 Connect and query
```python
from falkordb import FalkorDB
db = FalkorDB(host="localhost", port=6379)
g = db.select_graph("acme")                       # graph is created on first write
g.query("MERGE (:Service {name:$n})", {"n": "payment"})
rows = g.ro_query("MATCH (s:Service) RETURN s.name").result_set
db.list_graphs()                                  # [tested]
# also on Graph: copy, delete, explain, profile, create_node_range_index,
# create_node_unique_constraint, list_constraints; result has .run_time_ms
```
- **[doc]** Parameters use `$name`. Execution plans are cached per unique query text, so use parameters instead of string-formatting values in.
- **[doc]** Per-query timeout: `TIMEOUT <ms>` on the raw command (cannot exceed `TIMEOUT_MAX`).

### 3.3 Cypher we are allowed to use
- **[doc] clauses:** MATCH, OPTIONAL MATCH, WHERE, RETURN, ORDER BY, SKIP, LIMIT, CREATE, MERGE (+ ON CREATE SET / ON MATCH SET), DELETE, REMOVE, SET, WITH, UNION, UNWIND, FOREACH, CALL.
- **[doc] variable-length:** `-[:CALLS*1..4]->`. Bounds are optional; `[*3]` means exactly 3 hops. Path variable: `p = (a)-[:CALLS*1..3]->(b)`; `nodes(p)`, `relationships(p)`, `length(p)`.
- **[doc] functions confirmed present:** `collect`, `size`, `nodes`, `relationships`, `length`, `shortestPath`, `allShortestPaths`, `toFloat`, `coalesce`, `timestamp`, `range`, `reduce`, `any`/`all`/`none`/`single`, list comprehensions, `CASE WHEN`.
- **[doc] NOT supported:** label expressions in MATCH, the regex operator (`=~`), hex/octal numbers, temporal arithmetic. Don't use them.
- **[tested]** `IN` with a list parameter, list comprehensions, and `WHERE NOT (s)-[:CALLS]->()` all work.
- **[doc] MERGE:** one `MERGE` per entity, then a separate `MERGE` for the relationship. Merging a whole path can create duplicate nodes.

### 3.4 Shortest path
```cypher
MATCH (a:Service {name:$from}), (b:Service {name:$to})
WITH shortestPath((a)-[:CALLS*]->(b)) AS p
RETURN length(p), [n IN nodes(p) | n.name]
```
- **[doc]** Endpoints must already be matched; **no property filters inside the pattern**; relationship types allowed; returns one path (use `allShortestPaths` for all); `NULL` if none; min hops may only be 0 or 1.

### 3.5 Algorithms (exact signatures)
| Algorithm | Call | Yields |
|---|---|---|
| BFS | `CALL algo.bfs(startNode, maxDepth, 'CALLS') YIELD nodes, edges` | nodes (breadth-first order), edges |
| Shortest paths | `CALL algo.SPpaths({sourceNode:a, targetNode:g, relTypes:['CALLS'], weightProp:'w', pathCount:1}) YIELD path, pathWeight` | path, pathWeight (+ pathCost when `costProp` set). Optional: `maxLen`, `maxCost`, `relDirection` |
| PageRank | `CALL algo.pageRank('Service','CALLS') YIELD node, score` | node, score (sum to 1.0; direction matters) |
| Betweenness | `CALL algo.betweenness({nodeLabels:['Service'], relationshipTypes:['CALLS'], samplingSize:32, samplingSeed:1}) YIELD node, score` | node, score. Set `samplingSeed` for reproducible runs |

**[tested]** `algo.bfs`, `algo.pageRank`, `algo.betweenness` run with the signatures above. **[unknown]** `SPpaths` needs a `weightProp`; our edges have none. Either add `w:1` to every CALLS edge or just use `shortestPath()`. BFS lines up best with `blast_radius` ("everything within N hops").

### 3.6 Concurrency (this changes the plan; see 3.6.1)
- **[doc]** Only **one write query runs at a time per graph**; concurrent writes queue FIFO and never interleave.
- **[doc]** Read queries don't block each other; they use a shared thread pool (`THREAD_COUNT`, default = number of logical cores) and see a snapshot-like view from before any in-flight write.
- **[doc]** Any query that modifies the graph is **atomic**: all or nothing.
- **[tested]** 50 parallel `SET r.successes = r.successes + 1` → exactly 50 (about 21 ms total). 50 parallel `MERGE` on one key → exactly 1 node (about 23 ms). A read in one query followed by a write in another is still **not** atomic, so use CAS.
- **[inferred]** Graphs are independent keys, so writes to *different* tenants' graphs can proceed in parallel while writes to one graph serialize. Test it (step 10), don't claim it before.

#### 3.6.1 What this means for NextTrace
- The CAS handoff query (`WHERE i.version=$v AND i.status=$from SET ...`) is atomic as a single query, so exactly one racer wins. Still needed: it protects the **read-then-decide across queries** pattern agents use.
- `MERGE` for signals should behave idempotently under 50 parallel writers because writes serialize. **[inferred]**; the unit test is the proof.
- Expect p99 write latency to grow with the number of parallel writers on one graph (they queue). Report it honestly. "Per-tenant graphs let different customers write in parallel" is a stronger and truthful framing than "FalkorDB handles any concurrency".

### 3.7 Indexes, uniqueness, memory, housekeeping
- **[doc] index:** `CREATE INDEX FOR (s:Signal) ON (s.key)`. Used automatically for equality / range / prefix filters; **not** for `<>`. Check with `GRAPH.EXPLAIN`.
- **[doc] unique constraint:** create the index first, then `GRAPH.CONSTRAINT CREATE <graph> UNIQUE NODE Signal PROPERTIES 1 key`. It replies `PENDING`; poll `CALL db.constraints()` until `OPERATIONAL`. An index that backs a constraint can't be dropped.
- **[tested]** `MERGE (s:Signal {key:$k})` uses the index (`Node By Index Scan` in the plan). Python: `create_node_range_index`, `create_node_unique_constraint`; duplicate CREATE is rejected once `OPERATIONAL`.
- **[doc] memory per graph:** `GRAPH.MEMORY USAGE <graph> [SAMPLES n]` returns sizes in MB (`total_graph_sz_mb`, ...). Use this for the tenant-scale test.
- **[doc]** `GRAPH.LIST` lists graphs; `GRAPH.DELETE` removes one; `GRAPH.COPY` duplicates a graph (title only; read its page before relying on it; might be the fast way to create 1,000 tenants from a template).
- **[doc] known limitations:**
  1. A relationship in a MATCH that you never reference only checks that *at least one* exists. If you count parallel edges, reference the alias.
  2. `LIMIT` doesn't stop earlier `CREATE/SET/DELETE/MERGE` work from running.
  3. No aggregate functions inside pattern comprehensions; aggregate in a prior `WITH`.
- **[doc]** Self-hosting without Docker needs Redis 8+. We use Docker, so ignore it.

---

## 4. Build order (follow in sequence; don't skip ahead)

Each step has **Done when** (a check you can run) and a **time box**. If a step blows its box by 2×, stop and apply the "if stuck" rule at the bottom.

### Day 0–1: Oct 15 (afternoon) → Oct 16: get a real number

Today (Oct 15): S0 ✅, S1 ✅, then S2, S3, S4. Tomorrow morning: S5, S6.

**S0. Environment (30 min)** ✅ done Oct 15
1. Open Docker Desktop. Run the `docker run` from 3.1.
2. Open http://localhost:3000. Run `RETURN 1`.
3. Create the repo, venv or `package.json`, install the FalkorDB client, Anthropic SDK, FastAPI/Hono, and a test runner.
4. `export ANTHROPIC_API_KEY=...`; add `.env.example`; `.env` is gitignored.
5. Decide Python vs TS (section 2) and write it in the decision log (section 6).
- **Done when:** a script creates `(:Service {name:'x'})` in graph `scratch` and reads it back.
- **Browser check (you, 5 min):** open http://localhost:3000 and connect to `localhost:6379` (no password). Select graph `browser_check` and run `MATCH (a)-[r]->(b) RETURN a, r, b`: expect 6 services joined by 5 arrows. Click a node to see its properties. Then try `MATCH (a:Service {name:'frontend'}), (b:Service {name:'redis'}) WITH shortestPath((a)-[:CALLS*]->(b)) AS p RETURN p` and see whether the path is drawn. Note anything odd in the decision log.

**S1. Smoke tests of every doubtful feature (60 min)** ✅ done Oct 15: `uv run pytest tests/test_smoke.py -v` (12/12)
Write one script with asserts. Each line is one check:
1. `IN` operator and list comprehension: `MATCH (s:Service) WITH collect(s) AS bad UNWIND bad AS x RETURN x.name, x IN bad`
2. Variable-length traversal with `DISTINCT` over a 4-node chain
3. `shortestPath` with the endpoints matched first
4. `MERGE` twice on the same key → still 1 node
5. CAS: run the handoff query twice with the same `$v` → second returns **0 rows**
6. Parameters through the client (dict / `{params}`)
7. Index + unique constraint on `Signal.key`; wait for `OPERATIONAL`; a duplicate `CREATE` is rejected
8. `GRAPH.EXPLAIN` on the `MERGE`: does it use the index?
9. `GRAPH.MEMORY USAGE scratch`; `GRAPH.LIST` / client list call
10. **Persistence:** write data → `docker stop falkordb` → `docker start falkordb` → data present. Then repeat with `docker kill falkordb`.
11. 50 parallel writers into one graph complete, and note the wall time (baseline for the concurrency test)
- **Done when:** every check passes **or** its result is written into the cheat sheet above, replacing the `[unknown]` tag.

**S2. Get the data and answer the four unknowns (45 min)** ✅ done Oct 15. Findings: [docs/data_notes.md](docs/data_notes.md)
1. Download `RE1-OB.zip` from https://zenodo.org/records/14590730 (31 MB) into `data/` (gitignored). Unzip into its own folder.
2. Open **one** case. Write down in `docs/data_notes.md`: file layout, metric column names, sampling interval, **where the fault-injection time is**, **where the root-cause label is**, how many cases per fault type.
3. List the service names that appear in the metric columns. They must match the topology you encode next.
- **Done when:** `docs/data_notes.md` answers the four questions in project_plan.md §3.1.
- **If the injection time is missing:** use the split point the RCAEval loaders use, or go to the synthetic fallback (project_plan.md §3.4).

**S3. Topology and anonymization (45 min)**
1. Open https://github.com/GoogleCloudPlatform/microservices-demo and copy the call edges from its architecture diagram. Compare with project_plan.md §3.2, which was written from memory.
2. Write `topology/online_boutique.cypher` using one `MERGE` per service, then one per edge. Add `owner_team`, `tier`, `user_function`.
3. Create `anonymize.py`: a fixed seeded mapping `real → svc-a…`, saved to a file.
- **Done when:** the graph in the Browser matches the diagram and the mapping file is committed.

**S4. Alarms into the graph (90 min)**
1. `detect.py`: baseline vs test window, z-score, threshold 3. Freeze after tuning on a 10-case dev split.
2. Self-check: a planted spike in a synthetic series → exactly one signal.
3. `load_case.py`: create `Incident`, `Signal` (MERGE on key), `HAS_SIGNAL`, `ON`. **Never** write the label into the graph; labels stay in `labels/`.
- **Done when:** case N loads and the Browser shows red services for it.

**S5. Graph-only culprit finder + first score (60 min) → Day 1 checkpoint**
1. Implement `rca_candidates` (project_plan.md §6) in `graph/queries.py`.
2. `bench/score.py`: Acc@1, Acc@3 against `labels/`.
3. Run setup B on all eval cases.
- **Done when:** a results JSON exists with Acc@1 / Acc@3. **Commit it.**

**S6. LLM baselines A and A+ (60 min)**
- A: anonymized signal list as text. A+: same plus topology as an adjacency list. Same model, temperature 0, answer = ranked list of 3 IDs.
- **Day 1 checkpoint (end of day):** compare B vs A vs A+ and choose the story using project_plan.md §12. Write the decision in section 6.

### Day 2: Oct 17: agents, memory, coordination

**S7. Tools + investigator agent = setup C (3 h)**
Wrap `rca_candidates`, `blast_radius`, `explain_path`, `similar_incidents` as tools. Cap the loop at about 8 calls. Log every call with its **graph time** and **LLM time** separately (this is the "fast enough per step" evidence). Run setup C on the eval set.

**S8. Memory (2 h)**
Train/eval split (60/40, seeded, stratified). Resolve train cases into the graph (incident, cause, runbook outcome). Run C cold vs warm. Restart the process between the two.

**S9. Triage, Scribe, CAS handoff, tests (2.5 h)**
Three agents; handoff only through the `handoff` query. Tests: idempotent ingest, CAS race, no lost updates (single-query increment), isolation, latency table (project_plan.md §9.6).

**S10. Tenant scale test (60 min)**
Create N graphs from the same template (try `GRAPH.COPY` or a loop), N = 100 → 1,000 if the machine copes. Record creation time and `GRAPH.MEMORY USAGE` for a few. Run the culprit query on 5 random tenants; assert isolation. Also: 50 parallel writers to **one** graph vs 50 writers spread across 50 graphs; report both timings. Report whatever N works, and say so.

**S11. Minimal UI (3 h)**
One HTML page + Cytoscape.js: case picker, graph with red nodes and highlighted path, agent trace with per-step timings, graph-off toggle. Fallback if behind: FalkorDB Browser + terminal trace.

### Day 3: Oct 18: ship by 6 PM IST

**S12. Extras in order:** F3 → F1 → F2 → F4 → F5 → F6. Stop at noon.
**S13. Deploy:** FalkorDB Cloud (app.falkordb.cloud) + one app host, with replay mode. If it fights back for more than 1 hour, ship local setup + video.
**S14. README, fresh-clone test, video, blog, submit:** follow the checklist in instructions.md §7.

---

## 5. Rules so we don't get lost

1. **One source of truth per question.** Event rules → instructions.md. What to build → project_plan.md. Does FalkorDB support X → section 3 here. Update it when a smoke test settles an `[unknown]`.
2. **Never edit the benchmark after seeing results.** Detector threshold and prompts are frozen after the dev split.
3. **Labels stay out of the graph and out of prompts.** Always. If a result looks too good, check this first.
4. **Commit at the end of every step.** A tag per step makes it easy to roll back.
5. **If stuck for 45 minutes:** write what you tried in section 6, ask in the FalkorDB Discord, then take the fallback (synthetic data, Browser instead of custom UI, local instead of deploy).
6. **Order of sacrifice if time runs out:** F6 → F5 → F4 → UI polish → live deploy → second tenant. Never cut: benchmark B/A/A+/C, memory cold vs warm, README with model + every query, video.
7. **Every number in the README comes from a saved results file.** No numbers from memory.

---

## 6. Decision log (fill in as you go)

| Date/time | Decision | Why |
|---|---|---|
| Oct 15 | Language: **Python 3.12 + uv** | user's choice; pandas + RCAEval loaders |
| Oct 15 | Docker runtime: **OrbStack** | already installed |
| Oct 15 | Persistence: stop ✅; kill ❌ by default → **AOF on** → kill ✅ | demo restarts must not lose memory |
| Oct 15 | RE1-OB: label = folder name; injection time = `inject_time.txt` (epoch s) | see docs/data_notes.md |
| Oct 15 | Naive no-graph baseline scores 83% Acc@1 / 92% Acc@3 | add it as setup D; graph must beat it; loss faults are the weak spot (9/25) |
| | Day 1 checkpoint outcome | |
| | Tenant scale reached (N graphs, MB each) | |
