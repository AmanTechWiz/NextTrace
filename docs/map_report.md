# Map proof: NextTrace's map vs recorded real traffic

The map is built from the repo alone (config and code). The ground truth is recorded traffic from the public
RCAEval RE2 dataset ([Zenodo](https://zenodo.org/records/14590730), CC BY 4.0), which we did not write.
Regenerate: `uv run --with remotezip python -m bench.map_proof --fetch && uv run python -m bench.map_proof`.

## Summary

| System | Calls seen in traffic, found in the map | Endpoint calls seen in traffic, found | Google diagram |
|---|---|---|---|
| Train Ticket | 55/55 (100%) | 63/63 (100%) | – |
| Online Boutique | 9/9 (100%) | 11/11 (100%) | 16/16 (100%) |

## Train Ticket

Repo `train-ticket` at commit `350f62000e`. Traffic: 10 recorded runs, 291,342 cross-service calls.

**Service level**

- Calls seen in traffic and found in the map: **55/55 (100%)**.
- Missed: none.
- Seen in traffic between services the repo doesn't declare: none.
- The map has 160 calls. 61 can be checked against this traffic (both services ran and were traced); 55/61 (90%) of those were seen.
- Not checkable: 68 (caller has no spans in these traces), 30 (callee is a datastore or infra (not traced)), 1 (callee has no spans in these traces).

**Endpoint level**

- Routes parsed: 255. Call sites mapped: 171, linked to a route: 169/171 (99%).
- Endpoint calls seen in traffic and found in the map: **63/63 (100%)**.
- Missed: none.
- Calls to a route the callee does not declare (likely bugs):
  - `ts-rebook-service` sends `POST /api/v1/orderOtherService/orderOther/…` to `ts-order-other-service` — [code](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-rebook-service/src/main/java/rebook/service/RebookServiceImpl.java#L340)
  - `ts-rebook-service` sends `POST /api/v1/orderservice/order/…` to `ts-order-service` — [code](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-rebook-service/src/main/java/rebook/service/RebookServiceImpl.java#L340)

<details><summary>6 mapped calls not seen in this traffic (each with its code line)</summary>

- `ts-admin-basic-info-service` → `ts-contacts-service` — [code](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-admin-basic-info-service/src/main/java/adminbasic/service/AdminBasicInfoServiceImpl.java#L38)
- `ts-admin-basic-info-service` → `ts-station-service` — [code](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-admin-basic-info-service/src/main/java/adminbasic/service/AdminBasicInfoServiceImpl.java#L27)
- `ts-admin-basic-info-service` → `ts-train-service` — [code](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-admin-basic-info-service/src/main/java/adminbasic/service/AdminBasicInfoServiceImpl.java#L188)
- `ts-preserve-other-service` → `ts-consign-service` — [code](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-preserve-other-service/src/main/java/preserveOther/service/PreserveOtherServiceImpl.java#L401)
- `ts-preserve-service` → `ts-consign-service` — [code](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-preserve-service/src/main/java/preserve/service/PreserveServiceImpl.java#L395)
- `ts-user-service` → `ts-auth-service` — [code](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-user-service/src/main/java/user/service/impl/UserServiceImpl.java#L32)

</details>

## Online Boutique

Repo `boutique` at commit `38e7348eb2`. Traffic: 5 recorded runs, 851,748 cross-service calls.

**Service level**

- Calls seen in traffic and found in the map: **9/9 (100%)**.
- Missed: none.
- Seen in traffic between services the repo doesn't declare: none.
- The map has 17 calls. 9 can be checked against this traffic (both services ran and were traced); 9/9 (100%) of those were seen.
- Not checkable: 1 (callee is a datastore or infra (not traced)), 6 (callee has no spans in these traces), 1 (caller has no spans in these traces).

**Endpoint level**

- Routes parsed: 16. Call sites mapped: 20, linked to a route: 20/20 (100%).
- Endpoint calls seen in traffic and found in the map: **11/11 (100%)**.
- Missed: none.

## Hand check of calls not seen in traffic

Traffic only shows what the load generator exercised. All 6 mapped-but-unseen calls were read in the code: **6/6 are real calls**.

| System | Call | Verdict |
|---|---|---|
| Train Ticket | [`ts-admin-basic-info-service` → `ts-train-service`](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-admin-basic-info-service/src/main/java/adminbasic/service/AdminBasicInfoServiceImpl.java#L188) | real call in the code: DELETE /trains/{id}. Admin panel only. |
| Train Ticket | [`ts-admin-basic-info-service` → `ts-station-service`](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-admin-basic-info-service/src/main/java/adminbasic/service/AdminBasicInfoServiceImpl.java#L27) | real call in the code: `stations` URL used in 3 calls (lines 98, 114, 128). Admin panel only. |
| Train Ticket | [`ts-preserve-other-service` → `ts-consign-service`](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-preserve-other-service/src/main/java/preserveOther/service/PreserveOtherServiceImpl.java#L401) | real call in the code: same as preserve, for the other train type. |
| Train Ticket | [`ts-user-service` → `ts-auth-service`](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-user-service/src/main/java/user/service/impl/UserServiceImpl.java#L32) | real call in the code: AUTH_SERVICE_URI + "/users/" + userId (line 154). User-management action. |
| Train Ticket | [`ts-admin-basic-info-service` → `ts-contacts-service`](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-admin-basic-info-service/src/main/java/adminbasic/service/AdminBasicInfoServiceImpl.java#L38) | real call in the code: getAllContacts() GETs /contacts. Admin panel only. |
| Train Ticket | [`ts-preserve-service` → `ts-consign-service`](https://github.com/FudanSELab/train-ticket/blob/350f62000e6658e0e543730580c599d8558253e7/ts-preserve-service/src/main/java/preserve/service/PreserveServiceImpl.java#L395) | real call in the code: createConsign() POSTs /consigns, only when a booking adds a consignment. |

## Limits

- Traffic covers only what the load generator did during these runs, so unseen ≠ wrong.
- Databases and infra are not traced in this dataset, so calls to them are checked against config only.
- Some services have no spans at all (e.g. Boutique's cart, shipping and ad services were not instrumented in
  this dataset), so calls to or from them can't be checked here; Boutique's are covered by Google's diagram.
- Train Ticket is mapped at release v0.2.1, the version whose service names match the dataset.
