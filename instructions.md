# Instructions for agents working in this repo

Read this first. It is the single source of context for the hackathon and the project.

**Status:** building started **Oct 15, 2026, 2:10 PM IST**. S0 to S3 done; next is S4 (see BUILD_GUIDE.md and roadmap.html). Detailed build plan: [project_plan.md](project_plan.md). Verified FalkorDB facts + step-by-step build order: [BUILD_GUIDE.md](BUILD_GUIDE.md) (start here when building). Primer on graph DBs/Cypher for the builder: [graph-db-explained.html](graph-db-explained.html).

## Github commit and push instructions 

Git commit rules:
•⁠  ⁠Commit small and often, one logical change per commit, right after it works. Never one giant commit.
•⁠  ⁠Subject line: imperative mood, under 60 characters, no trailing period. Examples: "Add graph schema for services", "Fix empty result in path query", "Wire up demo button".
•⁠  ⁠Optional short body only when the why isn't obvious. Plain sentences, no bullet lists.
•⁠  ⁠Write like a busy human developer: specific and plain. Name the actual thing changed. No filler words like "enhance", "streamline", "robust", "comprehensive".
•⁠  ⁠No emojis, no em dashes, no "Co-authored-by" or "Generated with" trailers, no mention of AI in commit messages.
•⁠  ⁠Never commit broken code, secrets, .env files, or large generated files. Check git status and the diff before every commit.
•⁠  ⁠Do not rewrite history, squash, amend published commits or backdate. Real timestamps only.
•⁠  ⁠Mix of types is fine and natural: features, fixes, small refactors, docs, tests.

## 1. The event

- **Name:** Graph Hacks: Context for AI Agents (WeMakeDevs x FalkorDB)
- **Page:** https://www.wemakedevs.org/hackathons/falkordb (rules: `/rules`, resources: `/resources`)
- **Window:** Oct 15, 2026, 12:01 AM IST to **Oct 18, 2026, 11:59 PM IST** (= 18:29 UTC Oct 18)
- **Internal deadline:** submit by **Oct 18, 6:00 PM IST**. No new features after Oct 18 noon IST.
- **Team:** solo
- **Tracks entered:** T1 Agents That Act on Connected Data, T2 Agent Memory and Coordination (also name T3 Company Brain if ownership + incident reports are done)
- **Help:** FalkorDB Discord https://discord.com/invite/Y2mMT9VdMy

**Brief, in their words:** "Build an agent that acts on connected data, remembers what it learns, and can show how it got to each answer."

**What judges want (judgment; no rubric is published):** the graph does real work on every agent step; multi-hop Cypher and FalkorDB algorithms used as agent tools; memory that visibly changes behavior; decisions you can trace; a data model and queries the builder can explain live.

## 2. Hard rules (violating any of these risks disqualification)

1. FalkorDB is the primary graph DB and powers a central part of the product.
2. **Removal test:** "if the product still works once FalkorDB is taken out, the graph is decoration." A graph used only for visualisation does not qualify.
3. All main code and the FalkorDB implementation are new work written during the event. Libraries, templates and public datasets are fine.
4. Data: **public, synthetic, or our own only.** No private, personal, paywalled or login-gated data.
5. AI coding assistant use must be disclosed in the README. The builder must be able to explain the graph model, the queries, the architecture and the decisions, so keep the code simple and explainable.
6. Never add AI co-author or "Generated with" lines to commits or PRs (owner's rule). The README disclosure is separate and required.

## 3. The project: NextTrace

**Pitch (use this exact sentence everywhere):** When something breaks, NextTrace shows which service failed first, who it hurts, and what fixed it last time.

**The three demo beats (everything else is optional):**
1. **Find it:** a judge picks a failure; the path lights up to the culprit.
2. **Prove the graph matters:** switch the graph off and the answer gets worse, with the scoreboard next to it.
3. **Remember it:** restart, hit the same kind of failure, and it's faster because it remembers.

**Data**
- RCAEval RE1-OB: https://zenodo.org/records/14590730 (31 MB, CC BY 4.0, 125 labeled failure cases on Online Boutique; metrics only). Credit it in the README.
- Topology: Online Boutique call graph, hand-encoded from https://github.com/GoogleCloudPlatform/microservices-demo
- Stretch only: RE1-TT (Train Ticket, bigger topology).

**Non-negotiable experiment hygiene**
- Ground-truth root causes are **never** written into the graph the agent can read. Store them in a separate file used only by the scorer.
- Service names are **anonymized** (svc-a, svc-b, ...) in every benchmark run, so the LLM can't use what it already knows about Online Boutique.
- Every setup that gets compared uses the same anomaly detector.

**Graph schema** (one FalkorDB graph per tenant, e.g. `acme`, `globex`)
```
(:Service {name, owner_team, tier, user_function})-[:CALLS]->(:Service)
(:Signal {key, metric, zscore, ts})-[:ON]->(:Service)
(:Incident {id, status, version, owner_agent, fault_type, opened_at})
(:Incident)-[:HAS_SIGNAL]->(:Signal)
(:Incident)-[:SUSPECTS {rank, score}]->(:Service)
(:Incident)-[:CAUSED_BY]->(:Service)
(:Runbook {fault_type, steps, attempts, successes})
(:Incident)-[:APPLIED {worked}]->(:Runbook)
(:Agent {name})-[:WROTE {ts}]->(:Incident|:Hypothesis)
```
`user_function` is the plain-English job of a service ("checkout", "browse products") and drives the business-impact view.

**Agent tools (each one a Cypher query; list them all in the README)**
1. `rca_candidates`: anomalous services with no anomalous downstream dependency, ranked by how many anomalous callers they explain
2. `blast_radius`: upstream callers of a service, plus their owner teams and user functions
3. `explain_path`: shortest path from `frontend` to the root cause
4. `similar_incidents`: Jaccard overlap of signal signatures with past resolved incidents, plus the runbook that worked
5. `handoff`: compare-and-swap on `Incident.version` and `status`. Zero rows returned means retry.
6. Ingest: `MERGE` on the signal key, so parallel writers stay idempotent

**Agents:** Triage (parallel alert writers) → Investigator (uses the tools) → Scribe (records the resolution, updates runbook counts, pages the owner team via a mock webhook). Agents hand off only through the graph.

**Proof the judges will see (report real numbers; never invent them)**
1. Removal test as a benchmark: Acc@1 and Acc@3 on RE1-OB for (a) LLM only, (b) graph query only, (c) the graph agent
2. Memory: tool calls, tokens and time for repeat fault types, compared with a wiped-memory control
3. Concurrency: N parallel writers produce exactly 1 incident and 0 lost updates (assert-based test); report p50/p99 write latency
4. Isolation: queries on the `acme` graph never return data from `globex` (test)

## 4. Extra features (only after the core and the benchmark work)

Ranked by impact per hour. Hours are estimates, not measurements.

| # | Feature | What the user sees | Est. | Priority |
|---|---|---|---|---|
| F1 | "What if?" button | Click any service, ask "what if this dies?", the map lights up everything that would break | ~2h | **Do** |
| F2 | "Why?" links | Every claim in an answer links to the exact path + signals behind it | ~3h | **Do** |
| F3 | "I'm not sure" mode | Weak evidence → shows top 3 suspects with scores instead of one confident guess | ~1h | **Do** |
| F4 | Business impact | "Customers can't check out" instead of "cart service down" (uses `user_function`) | ~2h | If time |
| F5 | Auto incident report | After a fix: what broke, how it spread, what worked, written from the graph | ~2h | If time |
| F6 | MCP server | Claude Code / Cursor can ask NextTrace "what's broken?" | ~3h | Stretch |

**Do not build:** live fault-injection clusters, multiple dashboards, real Slack/PagerDuty integrations (a mock webhook is enough), extra datasets before RE1-OB is done.

## 5. Day 1 checkpoint (end of Oct 16)

Compare graph-only and LLM-only Acc@1 on RE1-OB.
- **Graph clearly wins:** keep the "graph is smarter" story.
- **Gap is small:** keep building, but change the story to "graph is faster, cheaper, and explains itself" (measure tokens, latency, and explanation quality), and consider the RE1-TT stretch where topology is bigger.
- **Data path broken** (can't build clean signals): use synthetic fault injection on the Online Boutique topology. Only switch to backup H1 if that also fails.

**Backup idea H1 "MedWeb":** polypharmacy guardian for caregivers. Synthetic patients, one graph per patient, multi-hop drug interaction chains. Only ever "ask your pharmacist", never medical advice. Check drug-data licenses first.

## 6. Plan (3 days)

| Day | Morning | Afternoon | Evening | Done when |
|---|---|---|---|---|
| **Oct 16** | FalkorDB in Docker, download RE1-OB, inspect a case (check injection time), encode topology + anonymization map | Loader → `Signal` nodes; ground truth kept outside the graph; `rca_candidates` across 125 cases | LLM-only baseline; **Day 1 checkpoint**; post organizer questions in Discord | First Acc@1 numbers for both setups |
| **Oct 17** | Tools 2–6; investigator agent; memory writes (episodic + runbooks) | Triage + Scribe agents, CAS handoff, idempotent ingest; full 3-setup benchmark; memory replay | Concurrency + isolation tests; minimal UI (case picker, path highlight, agent trace, graph-off toggle); F1 + F3 | Benchmark table + passing tests + clickable demo |
| **Oct 18** | F2; deploy (replay mode); README | Fresh-clone test; record video; blog post | **Submit by 6 PM IST** | Submission confirmed in the portal |

**Cut order if behind:** F6 → F5 → F4 → custom UI polish (fall back to FalkorDB Browser + terminal trace) → live deploy (local setup + video is allowed) → second tenant (keep one isolation test).
**Never cut:** benchmark 1, memory before/after, README with schema and Cypher, demo video.

## 7. Submission checklist

- [ ] Public GitHub repo, with no secrets committed (`.env` in `.gitignore`, `.env.example` provided)
- [ ] README:
  - [ ] pitch sentence + problem
  - [ ] architecture diagram
  - [ ] **graph data model**
  - [ ] **every Cypher query / algorithm used**
  - [ ] benchmark tables
  - [ ] setup steps (fresh clone → running in under 10 minutes)
  - [ ] data sources + licenses
  - [ ] AI assistant disclosure
- [ ] Demo video (under 3 minutes unless organizers say otherwise), public link that works in incognito
- [ ] Live deployment URL (replay mode so judges don't need an LLM key), or full local setup instructions
- [ ] Tracks named: T1, T2 (+T3 if justified)
- [ ] Blog post published (side prize: AirPods)
- [ ] Fresh-clone test: follow the README on a clean checkout before submitting

## 8. How to present

**Demo video script (3 min)**
- 0:00 Hook: "3am. 40 alerts. Which one is real?"
- 0:20 **Beat 1, find it:** pick a case ID with the answer hidden. Alerts stream in, nodes turn red, the agent's tool calls show, the path lights up, it names the root cause, who is hurt (business impact) and the owner team, and pages them. Click "why?" on the answer. Then reveal the ground truth.
- 1:15 **Beat 2, prove it:** flip "graph off" on the same case, then show the 125-case benchmark table. This is the removal test, measured.
- 1:45 **Beat 3, remember it:** restart the process, load a new case of the same fault type: the agent recalls the past incident and runbook with fewer tool calls (show the numbers).
- 2:15 Quick hits: "what if?" button; 50 concurrent writers → 1 incident; tenant isolation.
- 2:45 Architecture + Cypher, then end.

**Presentation rules**
- Lead with the problem and the live moment, not the tech stack.
- Show the graph doing work (paths, traversals), not just a pretty picture.
- Every claim on screen comes from a real run. If a result is weak, show it and explain it.
- Don't claim to be the first or the only one. Claim the measured removal test plus correct memory and coordination.
- Be ready to explain every query and design decision without notes.

## 9. Open questions for organizers (ask in Discord)

1. What is the judging rubric, and who are the judges?
2. Can one project win more than one track?
3. Is there a maximum video length?
4. Does building on Graphiti or GraphRAG-SDK count as "our main FalkorDB implementation"?
5. Blog prize: what's the deadline and where do we submit?
6. Is a deployment in replay mode (cached runs, no live LLM) acceptable?
7. When are results announced?

Record the answers here when they come in.

## 10. Links

- FalkorDB docs: https://docs.falkordb.com | Cypher: https://docs.falkordb.com/cypher | Algorithms: https://docs.falkordb.com/algorithms
- FalkorDB MCP servers: https://github.com/FalkorDB/FalkorDB-MCPServer, https://github.com/FalkorDB/falkordb-mcp
- RCAEval paper: https://arxiv.org/abs/2412.17015
- Docker (verify against the getting-started page): `docker run -p 6379:6379 -p 3000:3000 -it --rm falkordb/falkordb:latest` (Browser on :3000)
