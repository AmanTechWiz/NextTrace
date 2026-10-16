# Agent benchmark: the same agent with and without NextTrace

Claude Code headless (`claude -p --restricted`), model `claude-sonnet-5-5`, effort `medium`, on a checkout of Train
Ticket v0.2.1. Same prompt and file tools in both setups; the nexttrace setup also gets NextTrace's read-only
MCP tools over a map of the same checkout, built by the parser alone (no traffic).

- **Traffic recall**: callers seen in recorded real traffic (RCAEval RE2-TT) that the answer lists. Traffic
  misses many real callers (the UI is not traced), so this is a lower bound, not a full answer key.
- **Made up**: listed services without a valid citation. Each citation is checked against the checkout:
  the quoted code must be in that file near that line, inside the caller's folder, and name the target.

| Setup | Questions | Traffic recall | Claims | Made up | Invalid citations | Cost | Time | Turns |
|---|---|---|---|---|---|---|---|---|
| plain | 8 | 38/40 | 59 | 0 | 0 | $0.496 | 2.4 min | 28 |
| nexttrace | 8 | 40/40 | 64 | 0 | 0 | $0.496 | 2.7 min | 26 |

## Per question

| Question | Setup | Traffic found | Claims | Made up | Cost | Time |
|---|---|---|---|---|---|---|
| blast-ts-route-service | nexttrace | 9/9 | 14 | 0 | $0.091 | 28s |
| blast-ts-route-service | plain | 9/9 | 13 | 0 | $0.095 | 28s |
| blast-ts-seat-service | nexttrace | 6/6 | 10 | 0 | $0.082 | 25s |
| blast-ts-seat-service | plain | 4/6 | 7 | 0 | $0.096 | 34s |
| callers-ts-order-service | nexttrace | 5/5 | 10 | 0 | $0.075 | 34s |
| callers-ts-order-service | plain | 5/5 | 9 | 0 | $0.048 | 14s |
| callers-ts-route-service | nexttrace | 3/3 | 5 | 0 | $0.048 | 16s |
| callers-ts-route-service | plain | 3/3 | 5 | 0 | $0.038 | 10s |
| callers-ts-seat-service | nexttrace | 4/4 | 6 | 0 | $0.042 | 15s |
| callers-ts-seat-service | plain | 4/4 | 6 | 0 | $0.046 | 18s |
| callers-ts-station-service | nexttrace | 6/6 | 10 | 0 | $0.058 | 16s |
| callers-ts-station-service | plain | 6/6 | 10 | 0 | $0.052 | 14s |
| callers-ts-ticketinfo-service | nexttrace | 4/4 | 5 | 0 | $0.046 | 12s |
| callers-ts-ticketinfo-service | plain | 4/4 | 5 | 0 | $0.054 | 11s |
| callers-ts-train-service | nexttrace | 3/3 | 4 | 0 | $0.054 | 15s |
| callers-ts-train-service | plain | 3/3 | 4 | 0 | $0.067 | 15s |
