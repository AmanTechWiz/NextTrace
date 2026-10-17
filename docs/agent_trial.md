# Live trial: three Claude agents handle real recorded failures through the graph

Each agent (triage, investigator, scribe) is a separate headless Claude Code session (`claude-sonnet-5-5`, effort
`medium`, `--restricted`, NextTrace MCP tools only). They share no context: the graph `agent_trial` is the only
shared state. Cases are RCAEval RE1-TT recordings; metric files were renamed so the faulty service's name
never reached an agent. Total cost $1.04.

| Incident | Real cause (from the dataset) | Agents' cause | Correct | Status | Handoffs | Steps logged |
|---|---|---|---|---|---|---|
| INC-1 (incident-1) | ts-order-service | ts-order-service | yes | resolved | triage:opened → investigator:claimed → investigator:diagnosed → scribe:resolved | 3 |
| INC-2 (incident-2) | ts-order-service | ts-order-service | yes | resolved | triage:opened → investigator:claimed → investigator:diagnosed → scribe:resolved | 3 |
| INC-3 (incident-3) | ts-route-service | ts-route-service | yes | resolved | triage:opened → investigator:claimed → investigator:diagnosed → scribe:resolved | 3 |

**The obvious shortcut fails here.** Blaming the loudest alarm picks a victim in all three cases (ts-seat-service, ts-preserve-service, ts-admin-travel-service: services that call the culprit and wait on it). The investigator found the cause by following the map from the alarmed services to the service they all depend on. Incident 2 repeated incident 1's fault: the scribe recalled INC-1's fix from memory (runbook now 2/2).

**Limits.** Three incidents is a demonstration, not a benchmark. Fixes were not applied to a running system: `worked=true` records the scribe's decision, as each scribe noted in its own reply.

**INC-1** fix: Restart ts-order-service (rolling restart of its pods) to clear memory pressure, raise its memory limit and add replicas, then check that latency on its callers (ts-inside-payment-service, ts-seat-service, ts-security-service, ts-travel-service) and memory on ts-consign-service return to baseline. If the problem returns, check ts-order-service for a memory leak and slow queries.

- `ts-inside-payment-service -> ts-order-service | code | ts-inside-payment-service/src/main/java/inside_payment/service/InsidePaymentServiceImpl.java:328 | "http://ts-order-service:12031/api/v1/orderservice/order/status/" + orderId + "/" + orderStatus,`
- `ts-seat-service -> ts-order-service | code | ts-seat-service/src/main/java/seat/service/SeatServiceImpl.java:201 | "http://ts-order-service:12031/api/v1/orderservice/order/tickets",`
- `ts-security-service -> ts-order-service | code | ts-security-service/src/main/java/security/service/SecurityServiceImpl.java:119 | "http://ts-order-service:12031/api/v1/orderservice/order/security/" + checkDate + "/" + accountId,`
- `ts-travel-service -> ts-order-service | code | ts-travel-service/src/main/java/travel/service/TravelServiceImpl.java:318 | "http://ts-order-service:12031/api/v1/orderservice/order/" + departureTime + "/" + trip.getTripId().toString(),`

**INC-2** fix: Reused INC-1 fix (same cause ts-order-service, worked 1/1): restart and scale ts-order-service, then confirm latency recovers on its callers (ts-inside-payment, ts-seat, ts-security, ts-travel, ts-preserve, ts-ui-dashboard).

- `ts-inside-payment-service -> ts-order-service | code | ts-inside-payment-service/src/main/java/inside_payment/service/InsidePaymentServiceImpl.java:328 | "http://ts-order-service:12031/api/v1/orderservice/order/status/" + orderId + "/" + orderStatus,`
- `ts-seat-service -> ts-order-service | code | ts-seat-service/src/main/java/seat/service/SeatServiceImpl.java:201 | "http://ts-order-service:12031/api/v1/orderservice/order/tickets",`
- `ts-security-service -> ts-order-service | code | ts-security-service/src/main/java/security/service/SecurityServiceImpl.java:119 | "http://ts-order-service:12031/api/v1/orderservice/order/security/" + checkDate + "/" + accountId,`
- `ts-travel-service -> ts-order-service | code | ts-travel-service/src/main/java/travel/service/TravelServiceImpl.java:318 | "http://ts-order-service:12031/api/v1/orderservice/order/" + departureTime + "/" + trip.getTripId().toString(),`
- `ts-preserve-service -> ts-order-service | code | ts-preserve-service/src/main/java/preserve/service/PreserveServiceImpl.java:368 | "http://ts-order-service:12031/api/v1/orderservice/order",`
- `ts-ui-dashboard -> ts-order-service | config | ts-ui-dashboard/nginx.conf:111 | proxy_pass   http://ts-order-service:12031;`
- `ts-food-service -> ts-travel-service | code | ts-food-service/src/main/java/foodsearch/service/FoodServiceImpl.java:232 | "http://ts-travel-service:12346/api/v1/travelservice/routes/" + tripId,`
- `ts-admin-travel-service -> ts-travel-service | code | ts-admin-travel-service/src/main/java/admintravel/service/AdminTravelServiceImpl.java:105 | requestUrl = "http://ts-travel-service:12346/api/v1/travelservice/trips";`

**INC-3** fix: Restart and scale out ts-route-service (it shows CPU and memory saturation plus istio latency p50-p95). Raise its CPU/memory limits and add replicas, then confirm latency recovers on its callers: ts-basic-service (-> ticketinfo, via ts-ticketinfo-service), ts-travel-service (-> admin-travel, food) and ts-travel2-service (-> preserve-other). If it recurs, check ts-route-service for slow or unbounded route lookups (/api/v1/routeservice/routes/{id}) and heap growth.

- `ts-basic-service -> ts-route-service | code | ts-basic-service/src/main/java/fdse/microservice/service/BasicServiceImpl.java:155 | "http://ts-route-service:11178/api/v1/routeservice/routes/" + routeId,`
- `ts-ticketinfo-service -> ts-basic-service | code | ts-ticketinfo-service/src/main/java/ticketinfo/service/TicketInfoServiceImpl.java:26 | "http://ts-basic-service:15680/api/v1/basicservice/basic/travel",`
- `ts-travel-service -> ts-route-service | code | ts-travel-service/src/main/java/travel/service/TravelServiceImpl.java:436 | "http://ts-route-service:11178/api/v1/routeservice/routes/" + routeId,`
- `ts-admin-travel-service -> ts-travel-service | code | ts-admin-travel-service/src/main/java/admintravel/service/AdminTravelServiceImpl.java:105 | requestUrl = "http://ts-travel-service:12346/api/v1/travelservice/trips";`
- `ts-food-service -> ts-travel-service | code | ts-food-service/src/main/java/foodsearch/service/FoodServiceImpl.java:232 | "http://ts-travel-service:12346/api/v1/travelservice/routes/" + tripId,`
- `ts-travel2-service -> ts-route-service | code | ts-travel2-service/src/main/java/travel2/service/Travel2ServiceImpl.java:370 | "http://ts-route-service:11178/api/v1/routeservice/routes/" + routeId,`
- `ts-preserve-other-service -> ts-travel2-service | code | ts-preserve-other-service/src/main/java/preserveOther/service/PreserveOtherServiceImpl.java:345 | "http://ts-travel2-service:16346/api/v1/travel2service/trip_detail",`

