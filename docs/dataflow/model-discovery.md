<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# model-discovery. Model Discovery

A client authenticates to the gateway and receives the list of public labels it may pass as `model`: every configured pair alias followed by every direct model alias. The response is built entirely from configuration frozen at process startup; no router, compressor, transport, or backend is touched.

- **Entry point:** `GET /v1/models` → `create_app.models` (`src/switchyard_gateway/adapters/ingress.py:198`)
- **Trigger:** An operator or OpenAI-compatible client probes the gateway for the route and model aliases it may request.
- **Termination:** Returns to the initiator (`CLIENT`); the happy path has no server-side sink or dead-end.
- **Response:**
  - **Success:** `200` JSON `{"object":"list","data":[{"id":<alias>,"object":"model","created":0,"owned_by":"gateway"},...]}` plus `x-request-id`.
  - **Failure:** `401` JSON `{"error":{"message":"unauthorized","type":"gateway_error","code":"unauthorized"}}` plus `x-request-id`.
  - **Exception:** No dedicated generic-exception branch exists on the route; an unforeseen `GatewayError`-free exception would escape to the ASGI server default (see Anomalies and Serialization notes).

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `CLIENT` | 1 | initiator | — | External HTTP caller (operator or OpenAI-compatible client) that sends the discovery request and receives the model list. | — (external) |
| `HTTP_SERVER` | 7 | intermediary | `uvicorn.run` | Single Uvicorn ASGI worker hosting the FastAPI app; accepts the connection and dispatches the request to the route. | `src/switchyard_gateway/bootstrap.py` |
| `BOOTSTRAP` | 7 | intermediary | `bootstrap.main` | Composition root that parses CLI arguments, loads configuration, wires the Gateway, and starts the server. | `src/switchyard_gateway/bootstrap.py` |
| `CONFIG_FILE` | 7 | intermediary | — | The untracked JSONC configuration document (default `config.jsonc`) that declares model and pair labels. | `config.jsonc` (untracked) |
| `LOAD_CONFIG` | 4 | intermediary | `load_config` | Config adapter that parses JSONC, resolves `{env:...}` references, validates references, and builds `Settings`. | `src/switchyard_gateway/adapters/config.py` |
| `SETTINGS` | 3 | intermediary | `Settings` | Frozen gateway-owned settings dataclass holding the `models` and `pairs` mappings consumed by discovery. | `src/switchyard_gateway/domain.py` |
| `GATEWAY` | 3 | intermediary | `Gateway` | Application-core Gateway object that owns the frozen settings used to answer discovery. | `src/switchyard_gateway/application.py` |
| `MODELS_ROUTE` | 2 | intermediary | `create_app.models` | FastAPI `GET /v1/models` handler that authenticates the caller and serializes the alias list. | `src/switchyard_gateway/adapters/ingress.py` |
| `AUTHORIZE` | 2 | intermediary | `create_app.authorize` | Ingress helper that compares the `Authorization` header to `Bearer <api_key>` with a constant-time digest. | `src/switchyard_gateway/adapters/ingress.py` |
| `ERROR_RESPONSE` | 2 | intermediary | `_error` | Sanitized error response builder used only on the auth-failure branch. | `src/switchyard_gateway/adapters/ingress.py` |
| `UUID4` | 7 | intermediary | `uuid.uuid4` | CPython standard-library source of the per-request id returned in the `x-request-id` header. | `src/switchyard_gateway/adapters/ingress.py` |
| `EVENT_SINK` | 4 | intermediary | `JsonEvents` | JSON stdout event sink that the chat route emits to; never invoked by model discovery. | `src/switchyard_gateway/adapters/logging.py` |

## Sequence

1. (seq 1–8) At process start `bootstrap.main` calls `load_config(args.config)` (default `config.jsonc`); the config adapter reads the JSONC document, resolves `{env:...}` references, validates that pair and model labels do not collide, constructs a frozen `Settings`, and returns it to the composition root. `build_app` stores it on `Gateway`, and `uvicorn.run(..., workers=1, access_log=False)` starts one ASGI worker.
2. (seq 10) The client issues `GET /v1/models` with `Authorization: Bearer <key>`.
3. (seq 11) Uvicorn dispatches the request to the FastAPI route `create_app.models` (`src/switchyard_gateway/adapters/ingress.py:198`).
4. (seq 12–13) The handler mints `request_id = uuid.uuid4().hex` before authenticating (`src/switchyard_gateway/adapters/ingress.py:200`).
5. (seq 14–15) `authorize` accepts: `hmac.compare_digest` matches the header against `Bearer {gateway.settings.api_key}` (`src/switchyard_gateway/adapters/ingress.py:187`).
6. (seq 16–19) The handler reads `gateway.settings.pairs` and `gateway.settings.models` and concatenates their keys in configuration order, `names = [*pairs, *models]` (`src/switchyard_gateway/adapters/ingress.py:204`).
7. (seq 20) It returns `200` with `{"object":"list","data":[{"id":<alias>,"object":"model","created":0,"owned_by":"gateway"}...]}` and the `x-request-id` header (`src/switchyard_gateway/adapters/ingress.py:205`). Pairs precede direct models, so the fixture labels resolve to `["switchyard", "cheap", "expensive"]`.

## Error paths

### Unauthenticated discovery request

_Covers:_ seq 30, 31, 32, 33; `AUTHORIZE` → `MODELS_ROUTE` (outcome: error); `MODELS_ROUTE` → `ERROR_RESPONSE` (outcome: error); `ERROR_RESPONSE` → `MODELS_ROUTE` (outcome: error); `MODELS_ROUTE` → `CLIENT` (outcome: error)

A missing or incorrect `Authorization` header makes `hmac.compare_digest` fail, so `create_app.authorize` raises `GatewayError("unauthorized", 401)` (`src/switchyard_gateway/adapters/ingress.py:187`). The route's `except GatewayError` branch calls `_error`, which returns a `JSONResponse` carrying `{"message":"unauthorized","type":"gateway_error","code":"unauthorized"}` and `status_code=401` with a fresh `x-request-id` (`src/switchyard_gateway/adapters/ingress.py:101`). No config read, upstream contact, or state mutation occurs on this branch.

## Anomalies

### Discovery requests are invisible to the event sink

_Covers:_ seq 40; `MODELS_ROUTE` → `EVENT_SINK` (outcome: anomaly)

The chat handler emits a `request` event for early rejections (`src/switchyard_gateway/adapters/ingress.py:272`, `:282`) and exactly one terminal event per opened exchange via `Gateway.finish` (`src/switchyard_gateway/application.py:251`), and the README instructs operators to read JSON records from the container logs (`README.md` §Observability). The `models` handler contains no `gateway.events.emit` on either the `200` or the `401` path (`src/switchyard_gateway/adapters/ingress.py:198`), so both successful alias discovery and authentication-failure probes leave no record in the event stream. The test that pins a `401` without a token asserts only the status code and never an event (`tests/test_ingress.py:TestChatIngress.test_authentication_and_model_discovery`), so the gap is unpinned. This is a verified-absent path, not a handled failure: nothing errors, a record that every other inbound route produces is simply never written.

## Verification

- `src/switchyard_gateway/adapters/ingress.py:create_app.models` — reads `[*settings.pairs, *settings.models]`, emits one `{"id", "object":"model", "created":0, "owned_by":"gateway"}` row per alias, returns a `JSONResponse` with `x-request-id`; only `GatewayError` is caught.
- `src/switchyard_gateway/adapters/ingress.py:create_app.authorize` — `hmac.compare_digest(request.headers.get("authorization","").encode(), f"Bearer {settings.api_key}".encode())`; failure raises `GatewayError("unauthorized", 401)`.
- `src/switchyard_gateway/adapters/ingress.py:_error` — sanitized error JSON with `status_code=error.status` and `x-request-id`.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — `openapi_url=None` means the route is intentionally absent from generated docs.
- `src/switchyard_gateway/bootstrap.py:main` and `build_app` — default `--config config.jsonc`, `load_config`, `Gateway(settings, ...)`, `uvicorn.run(..., workers=1, access_log=False)`.
- `src/switchyard_gateway/adapters/config.py:load_config`, `Config.settings`, `Config.validate_references` — JSONC parse plus `{env:...}` resolution and the "model and pair labels collide" guard that makes duplicate list entries impossible.
- `src/switchyard_gateway/domain.py:Settings` — frozen dataclass exposing `models: dict[str, Model]` and `pairs: dict[str, Pair]`.
- `src/switchyard_gateway/application.py:Gateway` — stores `settings` and never emits an event outside `open`/`finish`, confirming discovery produces no telemetry.
- `tests/test_ingress.py:TestChatIngress.test_authentication_and_model_discovery` — pins `401` without a token and ordering `["switchyard", "cheap", "expensive"]` with the fixture key.
- `tests/test_live.py:TestLiveGateway.test_first_turn_and_escalation` — pins that a configured pair label appears in `GET /v1/models` data.
- `scripts/container_smoke.py` — pins `GET /v1/models == 200` with `Bearer test-client-key` in the built image.
- `README.md` §Client interface (`:93`) — documents `GET /v1/models`: "pair labels and direct model labels".

## Serialization notes

### Roles and termination

- `CLIENT` is `initiator` only, per initiator-wins: it both starts the lifecycle and receives the `200`/`401` return, so its termination is derived from incoming return edges rather than declared.
- No `sink` or `dead-end` node exists: every path terminates in a response to the initiator. The single anomaly is an absent side effect, not a termination, so `EVENT_SINK` stays `intermediary`.

### Seq ordering

- Startup provenance occupies seq 1–8 under `phase: "startup"` so the "configured" aliases have a traceable origin; request handling occupies seq 10–20, the auth failure 30–33, and the anomaly 40. Gaps are deliberate.
- `phase` is only filter sugar; `seq` remains the sole total order.

### Kind mapping

- Attribute reads of `gateway.settings` are modeled as `call`/`return` (seq 16–19) even though no port method is invoked; they are the data-dependency edges that explain where the listed aliases come from.
- The sole `note` edge (seq 40) documents a non-event; `multigraph` is set because `AUTHORIZE → MODELS_ROUTE` and `MODELS_ROUTE → CLIENT` each carry two parallel edges at different seqs.

### Candidate anomaly rejected

- The route lacks the chat handler's generic `except Exception` sanitizer, but every statement in the body either raises `GatewayError` or constructs a JSON-safe payload, so no code path exercises an unhandled escape. It was not recorded as an anomaly to avoid claiming an unexercisable path.

## References

### Specs and decisions

- [README.md §Client interface](README.md) — documents `GET /v1/models` as pair and direct model labels.
- [README.md §Observability](README.md) — states the JSON event records operators are expected to see; grounds the anomaly.

### Related lifecycles

- [chat-completion](chat-completion.md) — shares `authorize`, `_error`, and the
  event sink, but the chat route emits events while discovery does not.
- [health-check](health-check.md) — the other unauthenticated-adjacent route on
  the same `create_app` app.
- [cli-startup](cli-startup.md) — the operator path that binds the server this
  endpoint runs on.

### Code

- `src/switchyard_gateway/adapters/ingress.py:create_app.models` — the entry point and response construction.
- `src/switchyard_gateway/adapters/ingress.py:create_app.authorize` — bearer-token authentication.
- `src/switchyard_gateway/adapters/ingress.py:_error` — sanitized failure response.
- `src/switchyard_gateway/adapters/config.py:load_config` — configuration provenance for the alias list.
- `src/switchyard_gateway/domain.py:Settings` — the frozen data the response is derived from.
- `src/switchyard_gateway/bootstrap.py:main`, `build_app` — process startup and server launch.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents` — the event sink the route never calls.
