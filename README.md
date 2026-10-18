# NextTrace

**A shared, verified map of your services in FalkorDB, built from your repos, that every developer and AI agent can
query, extend with proof, and remember incidents in.**

Point it at a repo: it finds the services and who calls whom (down to the route and the code line), writes them into
FalkorDB, and serves them to agents over MCP. Agents use the graph to answer "what breaks if this fails?", fill in
what the parser missed (only with a quote the server verifies in the code), and hand incidents to each other from
alarm to fix, with every step remembered.

Built for Graph Hacks (WeMakeDevs x FalkorDB), tracks 1 and 2.

---

## What it does

| | |
|---|---|
| **Map a repo** | Services from Kubernetes, docker-compose, Backstage, .NET Aspire or Dockerfiles; calls from config, code (Java, Python, Node, Go, C#, gRPC) and recorded traces. Every connection keeps its evidence (`file:line` or traffic count) and confidence. Several repos share one graph. Re-mapping marks vanished calls stale instead of deleting them. |
| **Answer failure questions** | `blast_radius` (what breaks, multi-hop, with owners), `failure_paths` (how requests reach a service), `criticality` (FalkorDB `algo.pageRank` and `algo.betweenness`). Each answer cites the connections it used. |
| **Check an answer before it is given** | `check_answer`: every call an agent's answer relies on is looked up on the map. Live calls come back with the map's own evidence; calls not on the map, stale calls, unknown services and services not chained to the target are flagged. |
| **Let AI fill gaps, safely** | `find_gaps` lists what the parser may have missed. `add_connection`, `add_service`, `merge_services` and `set_service_folder` accept a change only if the quoted line exists in the code near the given line and names the service. Agent edits are stored as `inferred` and re-checked on every re-map. |
| **Handle incidents across agents** | Alarms (from a metrics file) become incidents grouped by how services link on the map. Agents move an incident `open → investigating → diagnosed → resolved` through version-checked claims; the cause must cite live map connections; fixes update runbook counts; past fixes are recalled; every agent session step is stored in the graph. |
| **Explore freely, read-only** | `query_graph_readonly` (your own Cypher, including FalkorDB algorithms), `get_graph_schema`, `list_graphs`. FalkorDB itself refuses writes on this path. |

## Setup (about 5 minutes)

```bash
# 1. FalkorDB (data kept in a volume; append-only file so nothing is lost on a crash)
docker run -d --name falkordb -v falkordb_data:/var/lib/falkordb/data -p 6379:6379 -p 3000:3000 \
  -e REDIS_ARGS="--appendonly yes --appendfsync everysec" falkordb/falkordb:latest

# 2. NextTrace
git clone https://github.com/AmanTechWiz/NextTrace && cd NextTrace
uv sync

# 3. Map a repo from the command line...
uv run python -m nexttrace.mapper /path/to/repo --graph myteam

# ...or use it from Claude Code: this repo's .mcp.json registers the server. Open Claude Code here and approve
# "nexttrace", then ask e.g. "map /path/to/repo into graph myteam, then what breaks if payments fails?"
```

FalkorDB Browser: http://localhost:3000. Connection settings: `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`,
`FALKORDB_PASSWORD`, `FALKORDB_TLS` (the same names as the official FalkorDB MCP server, so FalkorDB Cloud works).

Tests: `uv run pytest` (101 tests; FalkorDB must be running).

## Graph model

One graph per team or system (tenant). The map and the memory live in the same graph, so incidents connect to the
real services.

```
(:Service {name, kind, source_dir, owner, repo, origin, images, stale})
    -[:CALLS {sources, confidence, evidence[], evidence_count, repo, last_run, stale}]->(:Service)
    -[:EXPOSES]->(:Endpoint {key, name, method, path, where, declared})
    -[:CALLS_ENDPOINT {evidence[], matched}]->(:Endpoint)
(:MapRun {run, repo, root, commit, services, calls})

(:Alarm {key, service, signal, at})-[:ON]->(:Service)
(:Incident {id, key, title, status, version, owner, reason, evidence[], fix})
    -[:HAS_ALARM]->(:Alarm)   -[:AFFECTS]->(:Service)   -[:CAUSED_BY]->(:Service)   -[:FIXED_BY {worked}]->(:Runbook)
(:Event {seq, at, agent, action, from_status, to_status, note})-[:OF]->(:Incident)
(:Runbook {key, steps, attempts, successes})
(:Step {seq, at, tool, input, output, incident})-[:IN]->(:Session {id, agent, task})
```

`sources` is any of `trace` / `config` / `code` / `ai`; `confidence` is `high` / `medium` / `inferred`.
Unique constraints on `Service.name`, `Endpoint.key`, `Alarm.key`, `Incident.key`, `Runbook.key`, `Session.id` keep
parallel writers from creating duplicates.

## Queries and algorithms the product relies on

Blast radius: everything that depends on a service, shortest chain each, live edges only
([nexttrace/failure.py](nexttrace/failure.py)):
```cypher
MATCH (s:Service {name: $n})
MATCH p = (x:Service)-[:CALLS*1..6]->(s)
WHERE x <> s AND all(r IN relationships(p) WHERE NOT coalesce(r.stale, false))
WITH x, p ORDER BY length(p), [n IN nodes(p) | n.name]
WITH x, collect(p)[0] AS p
RETURN x.name, x.owner, length(p),
       [i IN range(0, length(p) - 1) | {caller: nodes(p)[i].name, callee: nodes(p)[i + 1].name,
        confidence: relationships(p)[i].confidence, evidence: relationships(p)[i].evidence[0]}]
```

Criticality: FalkorDB algorithms
```cypher
CALL algo.pageRank('Service', 'CALLS') YIELD node, score RETURN node.name, score
CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['CALLS'], samplingSize: $n, samplingSeed: 1})
     YIELD node, score RETURN node.name, score
```

Handoff: exactly one of several racing agents wins ([nexttrace/memory.py](nexttrace/memory.py)):
```cypher
MATCH (i:Incident {id: $id}) WHERE i.status = $from AND i.version = $v
SET i.status = $to, i.version = $v + 1, i.owner = $agent, i.updated_at = $at
RETURN i.version
```

Writing the map ([nexttrace/mapper.py](nexttrace/mapper.py)): `UNWIND ... MERGE (s:Service {name})`, then
`MERGE (a)-[c:CALLS]->(b) SET c.evidence = ...`, then everything this repo didn't produce in this run is marked
`stale = true`. Endpoint calls ([nexttrace/endpoints.py](nexttrace/endpoints.py)) link each call site to the
callee's route by method and path template.

## Results (all reproducible, all published with their misses)

| What | Result | Details |
|---|---|---|
| Map vs recorded real traffic (RCAEval RE2, 15 runs, 1.1M calls) | Train Ticket 55/55 calls and 63/63 endpoint calls found; Online Boutique 9/9 and 11/11; all 6 mapped-but-unseen calls read in code: 6/6 real | [docs/map_report.md](docs/map_report.md) |
| Unseen repos, answer key from the authors' own diagrams, written before running | parser alone 36/74 (49%); parser + Claude Code agent with the gap tools 57/74 (77%) | [docs/generalization.md](docs/generalization.md), [agent run](docs/generalization_agent.md) |
| Same agent alone, with code search, and with NextTrace, 8 dependency questions on Train Ticket | alone 28/40 traffic callers with 41 of 42 code citations invented; code search 38/40; with NextTrace 40/40; `check_answer` flagged exactly the 41 invented citations and none elsewhere; the graph alone 40/40 in milliseconds for $0 | [docs/agent_bench.md](docs/agent_bench.md) |
| Three Claude agents (separate sessions) hand off real recorded failures through the graph | 3/3 causes correct, where blaming the loudest alarm was wrong 3/3; the repeat failure reused the stored fix; $1.04 total | [docs/agent_trial.md](docs/agent_trial.md) |

**Limits, stated plainly.** Static mapping misses calls wired only at runtime (registries, Dapr, sagas) until an agent
proves them or traces confirm them. On grep-friendly code a plain agent answers direct-caller questions about as well;
NextTrace's edge is multi-hop chains, answers in milliseconds without a model, and knowledge that persists between
agents. The second agent run (69/74) used tools built after seeing those repos, so it is not an unseen score.

## Layout

```
nexttrace/   mapper.py endpoints.py failure.py memory.py alarms.py graphdb.py mcp_server.py
bench/       map_proof.py generalization.py agent_bench.py agent_trial.py (+ results/)
docs/        the reports above
tests/       unit and integration tests (MCP over stdio, parallel agents, tenant isolation)
```

## Data and credits

- Failure and traffic recordings: RCAEval (Pham et al., WWW 2025), https://zenodo.org/records/14590730, CC BY 4.0.
- Repos mapped: GoogleCloudPlatform/microservices-demo, FudanSELab/train-ticket (v0.2.1), microservices-demo
  (Sock Shop) and the eight repos listed in docs/generalization.md, all public.
- Inspired by the official FalkorDB MCP server's query and schema tools; this server is a separate Python
  implementation without an open write tool.

## AI disclosure

Built with Claude Code as a coding assistant. The design, the evaluation protocol (frozen code, answer keys written
from authors' docs before running, mutation-checked tests) and every published number were reviewed by the author.
