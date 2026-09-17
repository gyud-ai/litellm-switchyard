<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# health-check. Liveness and Readiness Check

An unauthenticated probe hits one of two health routes. `/health/liveliness` is a
static process check that returns `{"status": "ok"}` without reading gateway state.
`/health/readiness` asks `Gateway.ready()` whether any configured replica is
eligible and returns `503 {"status": "not_ready"}` when every replica is cooling
down, so an orchestrator can observe an "up but not serving" state.

- **Entry point:** `GET /health/liveliness` → `create_app.liveliness` and
  `GET /health/readiness` → `create_app.readiness`
  (`src/switchyard_gateway/adapters/ingress.py:201-209`)
- **Trigger:** A caller or orchestrator (curl, container healthcheck, load-balancer
  probe) sends an HTTP GET to one of the two paths.
- **Termination:** Sink at `HEALTH_ROUTE`; the response returns to the initiator
  `CLIENT`. `REPLICA_STATE` is read, never called out to a backend.
- **Response:**
  - **Success:** `HTTP 200` with body `{"status": "ok"}`; readiness only when at
    least one replica is eligible.
  - **Failure:** readiness returns `HTTP 503` with `{"status": "not_ready"}` when
    no replica is eligible. Unmatched paths or methods fall through to
    Starlette/FastAPI's default 404/405, which is outside this lifecycle.
  - **Exception:** None handled. `health()` has no I/O beyond the in-memory
    cooldown scan; an ASGI-stack failure would yield 500 or a dropped connection.

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `CLIENT` | 1 | initiator | — | External HTTP caller/operator sending an unauthenticated probe. | (external) |
| `UVICORN_SERVER` | 7 | intermediary | `uvicorn.run` | Single-process ASGI HTTP server bound to `settings.host:settings.port`. | `src/switchyard_gateway/bootstrap.py` |
| `FASTAPI_APP` | 2 | intermediary | `create_app` | FastAPI ASGI app; routes the path and serializes the handler result. | `src/switchyard_gateway/adapters/ingress.py` |
| `HEALTH_ROUTE` | 2 | sink | `create_app.readiness` | Handlers for both health paths; `liveliness()` is static, `readiness()` gates on `gateway.ready()`. | `src/switchyard_gateway/adapters/ingress.py` |
| `GATEWAY` | 3 | intermediary | `Gateway.ready` | Readiness predicate over configured replicas' cooldown deadlines. | `src/switchyard_gateway/application.py` |
| `REPLICA_STATE` | 3 | intermediary | `Gateway._cooldowns` | Process-local cooldown deadlines; absent entries are eligible. | `src/switchyard_gateway/application.py` |

Group legend: 1=Client, 2=Ingress, 3=Application core, 7=Process/OS.

## Sequence

1. (seq 1–3) `CLIENT` issues `GET /health/liveliness` or `GET /health/readiness` to
   `UVICORN_SERVER` at `settings.host:settings.port` (`bootstrap.py:main`). No
   `Authorization` header is checked; both routes are documented as
   unauthenticated (`README.md` §Client interface). `FASTAPI_APP` matches the path
   and dispatches to `HEALTH_ROUTE` (`ingress.py:201-209`).
2. (seq 4–6) For liveliness, `HEALTH_ROUTE` returns the literal dict
   `{"status": "ok"}` (`ingress.py:201-203`). It touches no port, no settings, and
   no replica state; `FASTAPI_APP` serializes it to `200 application/json`, and
   `UVICORN_SERVER` returns it to `CLIENT`.
3. (seq 7–10) For readiness, `HEALTH_ROUTE` calls `gateway.ready()`
   (`ingress.py:205-209`). `GATEWAY` scans `REPLICA_STATE` — the `_cooldowns`
   deadlines for every endpoint of every configured `Model` — and returns `True`
   when at least one deadline is at or before the monotonic clock
   (`application.py:89-96`). Endpoints with no entry were never cooled down and
   are eligible.
4. (seq 11–13) On the ready branch `HEALTH_ROUTE` returns `200 {"status": "ok"}`,
   which `FASTAPI_APP` and `UVICORN_SERVER` hand back to `CLIENT`.

## Error paths

### No replica is eligible

_Covers:_ seq 20, 21, 22

When every configured replica is cooling down, `Gateway.ready()`
(`application.py:89-96`) returns `False`, so `readiness()` returns
`503 {"status": "not_ready"}` (`ingress.py:205-209`). Replica cooldowns are set by
chat traffic in `Gateway._cooldown` (`application.py:111-123`) on connection
failures and on retryable `429/502/503/504` responses, so readiness degrades only
after the gateway has observed a replica failure. It is a request-routing
eligibility signal, not a transport probe: a backend that was never cooled down is
reported ready until a request fails. `_select` uses the same deadline comparison
(`application.py:98-109`), but readiness is aggregate across every configured
model: it is true while any model has an eligible replica, so a ready probe does
not guarantee that a request for a specific model will find one.

## Anomalies

None. The former "readiness is indistinguishable from liveness" anomaly is fixed:
`/health/readiness` no longer returns `{"status": "ok"}` unconditionally, and a
probe can now observe a `503 not_ready` state while the process is up.

## Verification

- `src/switchyard_gateway/adapters/ingress.py:create_app.liveliness` and
  `create_app.readiness` — confirmed the static `{"status": "ok"}` handler and the
  `gateway.ready()` gate with a `503 {"status": "not_ready"}` branch.
- `src/switchyard_gateway/application.py:Gateway.ready` — confirmed the
  `any(...)` scan over `settings.models` endpoints and `_cooldowns`, using the same
  deadline comparison as `_select`.
- `src/switchyard_gateway/application.py:Gateway._cooldown` — confirmed the only
  writer of `_cooldowns`, called from `_open` on connect failure and retryable
  status.
- `tests/test_application.py:TestReadiness` — pins the partial-eligibility
  predicate, the empty-model table, the exact cooldown-expiry boundary, and that a
  readiness probe emits no events.
- `tests/test_properties.py:test_readiness_tracks_eligible_replicas` — Hypothesis
  property: readiness is true exactly when at least one generated replica is not
  cooling down.
- `tests/test_ingress.py:TestHealthProbes` — pins `200 {"status":"ok"}`,
  `503 {"status":"not_ready"}` when every replica is cooling, recovery after
  cooldown expiry, and a static `200` liveliness.
- `README.md` §Client interface — documents the readiness contract.
- `scripts/container_smoke.py` — polls `/health/readiness` until `200` before
  exercising chat.
- `docs/dataflow/health-check.json` — validated by `check_graphs.py`.

## Serialization notes

### Role assignments

- `CLIENT` is `initiator` under the initiator-wins convention: it starts the
  lifecycle and receives the return edges (seq 6, 13, 22).
- `HEALTH_ROUTE` is `sink`: it is the server-side terminus where the intended
  response — including the 503 — is produced.
- `GATEWAY` and `REPLICA_STATE` are `intermediary`: they are read for the
  readiness decision and return a value; they do not terminate the lifecycle.
- No `dead-end` node remains: the readiness decision now terminates in a response.

### Seq ordering

- Liveliness occupies seq 1–6, the ready branch seq 7–13, and the not-ready branch
  seq 20–22. Gaps keep the subgraphs separable inside one total order.
- `multigraph` is `true` because `HEALTH_ROUTE → FASTAPI_APP`,
  `FASTAPI_APP → UVICORN_SERVER`, and `UVICORN_SERVER → CLIENT` each carry three
  parallel edges (liveliness, ready, not ready) at different seqs.

### Kind mapping

- Liveliness and readiness are modeled on one shared `HEALTH_ROUTE` node because
  both are unauthenticated probes of the same app, but their edges are distinct so
  the static and readiness-gated paths stay filterable.
- The not-ready links (seq 20–22) are `outcome: "error"`: the 503 is a handled,
  surfaced failure, not an anomaly.

## References

### Specs and decisions

- [Data Flow Graph schema](data-flow-graph.schema.json) — contract stamped as
  `urn:data-flow-graph:schema:3` (root `version: 3`).
- [README](../../README.md) §Client interface — documents both health routes and
  the readiness eligibility contract.
- [README](../../README.md) §Configuration — `cooldown_seconds` and the
  single-worker, process-local cooldown table.
- [README](../../README.md) §Quickstart — uses `curl .../health/readiness` as the
  container readiness command.

### Related lifecycles

- [cli-startup](cli-startup.md) and [app-lifespan](app-lifespan.md) — the process
  must load config and enter the lifespan before either probe can succeed.
- [model-discovery](model-discovery.md) — the other unauthenticated-adjacent
  route on the same FastAPI app.
- [chat-completion](chat-completion.md) and [chat-stream](chat-stream.md) — the
  traffic that writes the replica cooldowns readiness reads.

### Code

- `src/switchyard_gateway/adapters/ingress.py:create_app.readiness` — the route
  handlers.
- `src/switchyard_gateway/application.py:Gateway.ready` — the readiness predicate.
- `src/switchyard_gateway/application.py:Gateway._cooldown` — the cooldown writer.
- `src/switchyard_gateway/bootstrap.py:main` — server startup and bind address.
- `src/switchyard_gateway/domain.py:Endpoint` — replica type scanned by readiness.
- `src/switchyard_gateway/ports.py:BackendTransport` — backend-access port that
  readiness deliberately does not probe.
