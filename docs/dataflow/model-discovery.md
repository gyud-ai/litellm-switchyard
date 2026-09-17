<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# model-discovery. Model Discovery

A client authenticates to the gateway and receives the list of public labels it may pass as `model`: every configured pair alias followed by every direct model alias. The response is built entirely from configuration frozen at process startup; no router, compressor, transport, or backend is touched. Both the success and the authentication-failure branch emit one sanitized request event to the JSON sink.

- **Entry point:** `GET /v1/models` → `create_app.models` (`src/switchyard_gateway/adapters/ingress.py:211`)
- **Trigger:** An operator or OpenAI-compatible client probes the gateway for the route and model aliases it may request.
- **Termination:** Returns to the initiator (`CLIENT`); the happy path has no server-side sink, but both outcomes emit to `EVENT_SINK` (a side effect, not a return).
- **Response:**
  - **Success:** `200` JSON `{"object":"list","data":[{"id":<alias>,"object":"model","created":0,"owned_by":"gateway"},...]}` plus `x-request-id`, preceded by a `request` event with `outcome:"completed"`.
  - **Failure:** `401` JSON `{"error":{"message":"unauthorized","type":"gateway_error","code":"unauthorized"}}` plus `x-request-id`, preceded by a `request` event with `outcome:"rejected"`.
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
| `GATEWAY` | 3 | intermediary | `Gateway` | Application-core Gateway object that owns the frozen settings used to answer discovery and the event sink. | `src/switchyard_gateway/application.py` |
| `MODELS_ROUTE` | 2 | intermediary | `create_app.models` | FastAPI `GET /v1/models` handler that authenticates the caller, emits one request event, and serializes the alias list. | `src/switchyard_gateway/adapters/ingress.py` |
| `AUTHORIZE` | 2 | intermediary | `create_app.authorize` | Ingress helper that compares the `Authorization` header to `Bearer <api_key>` with a constant-time digest. | `src/switchyard_gateway/adapters/ingress.py` |
| `ERROR_RESPONSE` | 2 | intermediary | `_error` | Sanitized error response builder used only on the auth-failure branch. | `src/switchyard_gateway/adapters/ingress.py` |
| `UUID4` | 7 | intermediary | `uuid.uuid4` | CPython standard-library source of the per-request id returned in the `x-request-id` header and the event. | `src/switchyard_gateway/adapters/ingress.py` |
| `EVENT_SINK` | 4 | intermediary | `JsonEvents` | JSON stdout event sink that both discovery outcomes emit to, matching the chat route's request/rejected records. | `src/switchyard_gateway/adapters/logging.py` |

## Sequence

1. (seq 1–8) At process start `bootstrap.main` calls `load_config(args.config)` (default `config.jsonc`); the config adapter reads the JSONC document, resolves `{env:...}` references, validates that pair and model labels do not collide, constructs a frozen `Settings`, and returns it to the composition root. `build_app` stores it on `Gateway`, and `uvicorn.run(..., workers=1, access_log=False)` starts one ASGI worker.
2. (seq 10) The client issues `GET /v1/models` with `Authorization: Bearer <key>`.
3. (seq 11) Uvicorn dispatches the request to the FastAPI route `create_app.models` (`src/switchyard_gateway/adapters/ingress.py:211`).
4. (seq 12–13) The handler mints `request_id = uuid.uuid4().hex` before authenticating (`src/switchyard_gateway/adapters/ingress.py:213`).
5. (seq 14–15) `authorize` accepts: `hmac.compare_digest` matches the header against `Bearer {gateway.settings.api_key}` (`src/switchyard_gateway/adapters/ingress.py:195`).
6. (seq 16–19) The handler reads `gateway.settings.pairs` and `gateway.settings.models` and concatenates their keys in configuration order, `names = [*pairs, *models]` (`src/switchyard_gateway/adapters/ingress.py:217`).
7. (seq 20) It emits `{"event":"request","request_id":...,"status":200,"outcome":"completed"}` through `gateway.events.emit` (`src/switchyard_gateway/adapters/ingress.py:218-225`). The record carries no aliases and no request data.
8. (seq 21) It returns `200` with `{"object":"list","data":[{"id":<alias>,"object":"model","created":0,"owned_by":"gateway"}...]}` and the `x-request-id` header (`src/switchyard_gateway/adapters/ingress.py:226-235`). Pairs precede direct models, so the fixture labels resolve to `["switchyard", "cheap", "expensive"]`.

## Error paths

### Unauthenticated discovery request

_Covers:_ seq 30, 31, 32, 33, 34

A missing or incorrect `Authorization` header makes `hmac.compare_digest` fail, so `create_app.authorize` raises `GatewayError("unauthorized", 401)` (`src/switchyard_gateway/adapters/ingress.py:195`). The route's `except GatewayError` branch calls `_error`, which returns a `JSONResponse` carrying `{"message":"unauthorized","type":"gateway_error","code":"unauthorized"}` and `status_code=401` with a fresh `x-request-id` (`src/switchyard_gateway/adapters/ingress.py:105`), and emits the sanitized `{"event":"request","request_id":...,"status":401,"outcome":"rejected","error":"unauthorized"}` record before the response leaves the handler (`ingress.py:236-246`). No config read, upstream contact, or state mutation occurs on this branch.

## Anomalies

None. The former "discovery requests are invisible to the event sink" anomaly is fixed: both the `200` and the `401` path emit exactly one sanitized `request` event, matching the chat handler's `outcome:"rejected"`/terminal event vocabulary.

The candidate anomaly from the original trace still applies but remains unexercised: the route lacks the chat handler's generic `except Exception` sanitizer, yet every statement in the body either raises `GatewayError` or constructs a JSON-safe payload, so no path exercises an unhandled escape. It is not recorded as an anomaly to avoid claiming an unexercisable path.

## Verification

- `src/switchyard_gateway/adapters/ingress.py:create_app.models` — reads `[*settings.pairs, *settings.models]`, emits one completed request event on success and one rejected request event on auth failure, returns a `JSONResponse` with `x-request-id`; only `GatewayError` is caught.
- `src/switchyard_gateway/adapters/ingress.py:create_app.authorize` — `hmac.compare_digest(request.headers.get("authorization","").encode(), f"Bearer {settings.api_key}".encode())`; failure raises `GatewayError("unauthorized", 401)`.
- `src/switchyard_gateway/adapters/ingress.py:_error` — sanitized error JSON with `status_code=error.status` and `x-request-id`.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — `openapi_url=None` means the route is intentionally absent from generated docs.
- `src/switchyard_gateway/bootstrap.py:main` and `build_app` — default `--config config.jsonc`, `load_config`, `Gateway(settings, ...)`, `uvicorn.run(..., workers=1, access_log=False)`.
- `src/switchyard_gateway/adapters/config.py:load_config`, `Config.settings`, `Config.validate_references` — JSONC parse plus `{env:...}` resolution and the "model and pair labels collide" guard that makes duplicate list entries impossible.
- `src/switchyard_gateway/domain.py:Settings` — frozen dataclass exposing `models: dict[str, Model]` and `pairs: dict[str, Pair]`.
- `tests/test_ingress.py:TestModelDiscoveryEvents` — pins one event per outcome: `rejected`/401 with `error=unauthorized` and `completed`/200.
- `tests/test_ingress.py:test_every_discovery_request_emits_exactly_one_event` — Hypothesis property over authorized/unauthorized callers.
- `tests/test_ingress.py:TestChatIngress.test_authentication_and_model_discovery` — pins `401` without a token and ordering `["switchyard", "cheap", "expensive"]` with the fixture key.
- `tests/test_live.py:TestLiveGateway.test_first_turn_and_escalation` — pins that a configured pair label appears in `GET /v1/models` data.
- `scripts/container_smoke.py` — pins `GET /v1/models == 200` with `Bearer test-client-key` in the built image.
- `README.md` §Client interface (`:93`) — documents `GET /v1/models`: "pair labels and direct model labels".
- `README.md` §Observability — documents the JSON records operators read from container logs.

## Serialization notes

### Roles and termination

- `CLIENT` is `initiator` only, per initiator-wins: it both starts the lifecycle and receives the `200`/`401` return, so its termination is derived from incoming return edges rather than declared.
- No `sink` or `dead-end` node exists: every path terminates in a response to the initiator. `EVENT_SINK` stays `intermediary` because the event is a side effect alongside the response, not the route's termination.

### Seq ordering

- Startup provenance occupies seq 1–8 under `phase: "startup"` so the "configured" aliases have a traceable origin; request handling occupies seq 10–21, and the auth failure 30–34. Gaps are deliberate.
- The success event (seq 20) sits after the alias read (seq 16–19) and before the 200 return (seq 21) because the emit happens before `JSONResponse` is constructed; `seq` remains the sole total order.

### Kind mapping

- Attribute reads of `gateway.settings` and the event emit are modeled as `call`/`return` and `event` even though no port method is invoked; they are the data-dependency edges that explain where the listed aliases come from and where the record goes.
- `multigraph` is set because `AUTHORIZE → MODELS_ROUTE` and `MODELS_ROUTE → CLIENT` each carry two parallel edges at different seqs.

## References

### Specs and decisions

- [README.md §Client interface](README.md) — documents `GET /v1/models` as pair and direct model labels.
- [README.md §Observability](README.md) — states the JSON event records operators are expected to see.

### Related lifecycles

- [chat-completion](chat-completion.md) — shares `authorize`, `_error`, and the
  event sink, and both routes now emit one request event per call.
- [health-check](health-check.md) — the other unauthenticated-adjacent route on
  the same `create_app` app.
- [cli-startup](cli-startup.md) — the operator path that binds the server this
  endpoint runs on.

### Code

- `src/switchyard_gateway/adapters/ingress.py:create_app.models` — the entry point, event emission, and response construction.
- `src/switchyard_gateway/adapters/ingress.py:create_app.authorize` — bearer-token authentication.
- `src/switchyard_gateway/adapters/ingress.py:_error` — sanitized failure response.
- `src/switchyard_gateway/adapters/config.py:load_config` — configuration provenance for the alias list.
- `src/switchyard_gateway/domain.py:Settings` — the frozen data the response is derived from.
- `src/switchyard_gateway/bootstrap.py:main`, `build_app` — process startup and server launch.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents` — the event sink both outcomes emit to.
