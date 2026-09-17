<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# health-check. Liveness and Readiness Check

An unauthenticated probe hits one of two static health routes and receives a fixed
`{"status": "ok"}` JSON body. The handler reads no configuration, opens no
connection, and reports the same answer regardless of backend reachability.

- **Entry point:** `GET /health/liveliness` and `GET /health/readiness` —
  `src/switchyard_gateway/adapters/ingress.py:create_app.health`
- **Trigger:** A caller or orchestrator (curl, container healthcheck, load-balancer
  probe) sends an HTTP GET to one of the two paths.
- **Termination:** Sink at `HEALTH_ROUTE` (`health()`); the response returns to the
  initiator `CLIENT`. The `BACKEND_REPLICAS` node is a dead-end only inside the
  readiness-anomaly subgraph.
- **Response:**
  - **Success:** `HTTP 200` with body `{"status": "ok"}`.
  - **Failure:** None handled by this handler. Unmatched paths or methods fall through
    to Starlette/FastAPI's default 404/405, which is outside this lifecycle.
  - **Exception:** No I/O and no `try`/`except` in `health()`; there is no realistic
    exception path. An ASGI-stack failure would yield 500 or a dropped connection.

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `CLIENT` | 1 | initiator | — | External HTTP caller/operator sending an unauthenticated probe. | (external) |
| `UVICORN_SERVER` | 7 | intermediary | `uvicorn.run` | Single-process ASGI HTTP server bound to `settings.host:settings.port`. | `src/switchyard_gateway/bootstrap.py` |
| `FASTAPI_APP` | 2 | intermediary | `create_app` | FastAPI ASGI app; routes the path and serializes the handler result. | `src/switchyard_gateway/adapters/ingress.py` |
| `HEALTH_ROUTE` | 2 | sink | `health` | Handler for both health paths; returns `{"status": "ok"}` unconditionally. | `src/switchyard_gateway/adapters/ingress.py` |
| `BACKEND_REPLICAS` | 6 | dead-end | `Endpoint` | Backend replicas used by chat traffic but never consulted by readiness. | `src/switchyard_gateway/domain.py` |

Group legend: 1=Client, 2=Ingress, 6=External backend, 7=Process/OS.

## Sequence

1. (seq 1) `CLIENT` issues `GET /health/liveliness` or `GET /health/readiness` to
   `UVICORN_SERVER` at `settings.host:settings.port` (`bootstrap.py:main`). No
   `Authorization` header is checked; the README documents both as unauthenticated
   process checks (`README.md` §Client interface).
2. (seq 2) `UVICORN_SERVER` hands the ASGI HTTP scope (method, path) to
   `FASTAPI_APP` built by `create_app` (`ingress.py:create_app`).
3. (seq 3) `FASTAPI_APP` matches the path and dispatches to `HEALTH_ROUTE`. Both
   decorators (`@app.get("/health/liveliness")`, `@app.get("/health/readiness")`)
   stack on the single `health` function, so the two URLs are interchangeable.
4. (seq 4) `HEALTH_ROUTE` returns the literal dict `{"status": "ok"}`
   (`ingress.py:create_app.health`). It touches no port, no settings, and no backend.
5. (seq 5) `FASTAPI_APP` serializes the dict to a `200 application/json` response.
6. (seq 6) `UVICORN_SERVER` returns the response to `CLIENT`.

## Error paths

None. `health()` has no validation, no `GatewayError` handling, and no failing
branch; the two routes always return `{"status": "ok"}`.

## Anomalies

### Readiness is indistinguishable from liveness

_Covers:_ seq 7, `HEALTH_ROUTE` → `BACKEND_REPLICAS` (outcome: anomaly)

`/health/readiness` returns `{"status": "ok"}` unconditionally
(`src/switchyard_gateway/adapters/ingress.py:create_app.health`): it never consults
`gateway.settings`, the compressor, or `BackendTransport` (`ports.py:BackendTransport`).
The same handler serves `/health/liveliness`, so a readiness probe can report ready
while every configured `Endpoint` (`domain.py:Endpoint`) is unreachable and chat
requests would fail or return 503. This is intentional per the README's
"unauthenticated process checks" (`README.md` §Client interface), but the readiness
name promises a serving-capability signal the implementation does not provide, which
is why the branch is recorded as an anomaly rather than a happy path.

The two-state behavior is reinforced at startup: configuration is loaded and
validated before `uvicorn.run` is called, and a failure raises `SystemExit(1)`
without binding the port (`bootstrap.py:main`). There is therefore no HTTP state in
which the process is up but reports "not ready" — a probe only ever sees
connection-refused or `200 ok`.

## Verification

- `src/switchyard_gateway/adapters/ingress.py:create_app.health` — confirmed both
  `@app.get` decorators stack on one handler returning `{"status": "ok"}` with no
  gateway access.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — confirmed the FastAPI app
  is built here with `docs_url`/`openapi_url` disabled and routes registered in-order.
- `src/switchyard_gateway/bootstrap.py:main` — confirmed `uvicorn.run(..., workers=1)`
  binds `settings.host`/`settings.port` and that config failure exits before serving.
- `src/switchyard_gateway/bootstrap.py:build_app` — confirmed backends live behind
  `HttpxTransport` and are never handed to the health handler.
- `src/switchyard_gateway/ports.py:BackendTransport` — confirmed backend access is a
  port, so readiness could check it but does not.
- `src/switchyard_gateway/domain.py:Endpoint` — confirmed replica type backing
  `BACKEND_REPLICAS`.
- `tests/test_ingress.py:TestChatIngress.test_authentication_and_model_discovery` —
  pins `GET /health/readiness == 200` with no credentials.
- `scripts/container_smoke.py` — container readiness gate polls `/health/readiness`
  until it returns 200 before exercising chat.
- `tests/test_live.py` — live suite uses `/health/readiness` as its preflight check.

## Serialization notes

### Role assignments

- `CLIENT` is `initiator` under the initiator-wins convention: it both starts the
  lifecycle and receives the return edge (seq 6); termination there is derived, not
  declared.
- `HEALTH_ROUTE` is `sink`: it is the legitimate server-side terminus where the
  intended response is produced (seq 4).
- `BACKEND_REPLICAS` is `dead-end` only within the readiness-anomaly subgraph: the
  expected readiness probe terminates there with no response.
- `UVICORN_SERVER` and `FASTAPI_APP` are `intermediary`.

### Seq ordering

- seq 1–6 form the single synchronous request/response order. The two health URLs
  share this range rather than duplicating links, because they are interchangeable
  and map to the same handler; `multigraph` is left `false`.
- seq 7 is placed after the response to keep the happy-path order contiguous.

### Kind mapping

- seq 1–3 are `call`; seq 4–6 are `return`.
- seq 7 is `note`, not `call`, because no runtime message is ever exchanged — it
  annotates an expected-but-absent interaction.

## References

### Specs and decisions

- [Data Flow Graph schema](data-flow-graph.schema.json) — contract stamped as
  `urn:data-flow-graph:schema:3` (root `version: 3`).
- [README](../../README.md) §Client interface — documents both health routes as
  unauthenticated process checks.
- [README](../../README.md) §Quickstart — uses `curl .../health/readiness` as the
  container readiness command.

### Related lifecycles

- [cli-startup](cli-startup.md) and [app-lifespan](app-lifespan.md) — the process
  must load config and enter the lifespan before either probe can succeed; a
  probe only ever sees connection-refused or `200 ok`.
- [model-discovery](model-discovery.md) — the other unauthenticated-adjacent
  route on the same FastAPI app; it shares `create_app` and the error helper.
- [chat-completion](chat-completion.md) and [chat-stream](chat-stream.md) — the
  traffic whose backend reachability `/health/readiness` does not reflect.

### Code

- `src/switchyard_gateway/adapters/ingress.py:create_app.health` — the route handler.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — app assembly and route
  registration.
- `src/switchyard_gateway/bootstrap.py:main` — server startup and bind address.
- `src/switchyard_gateway/bootstrap.py:build_app` — adapter wiring for backends.
- `src/switchyard_gateway/domain.py:Endpoint` — replica type.
- `src/switchyard_gateway/ports.py:BackendTransport` — backend-access port.
