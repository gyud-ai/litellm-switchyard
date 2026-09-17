<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# chat-completion. Authenticate, route, compress, forward, and rewrite a non-streaming completion

An authenticated Chat Completions client posts a non-streaming request; the gateway validates
it, optionally routes the alias through Switchyard, protects and compresses eligible history
through Headroom, selects one replica, opens exactly one upstream exchange, checks the upstream
`content-type` is JSON, buffers the JSON completion, rewrites `model` to the requested alias
while capturing provider `usage`, closes the upstream, and emits exactly one terminal event.

- **Entry point:** `POST /v1/chat/completions` (no `stream`, or `stream: false`) handled by `create_app.chat` in `src/switchyard_gateway/adapters/ingress.py:249`
- **Trigger:** An authenticated caller sends a Chat Completions payload that is not a streaming request
- **Termination:** Sink node `EVENTS` (terminal record) after `GATEWAY_FINISH`; the JSON completion itself returns to the `CLIENT` initiator. `uvicorn.run` (`bootstrap.py:98`) owns the process lifetime.
- **Response:**
  - **Success:** HTTP 200 JSON body with upstream `choices` intact, `model` rewritten to the requested alias, provider `usage` captured into the terminal event, and `x-gateway-model`/`x-gateway-endpoint` headers
  - **Failure:** A sanitized JSON error body (`_error`, `ingress.py:105`) with `x-request-id` and a status of 400/401/404/413/500/502/503/504; a wrong-type 200 is surfaced as the specific `invalid_upstream_content_type` 502
  - **Exception:** An unhandled escape in the route or a cleanup failure in the terminal `finally` propagates to Starlette; the upstream is not leaked because `Gateway.finish` closes before emitting

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `CLIENT` | 1 | initiator | — | External OpenAI-compatible HTTP caller that posts a non-streaming Chat Completions request and consumes the JSON completion. | external HTTP caller |
| `UVICORN` | 7 | intermediary | `uvicorn.run` | Single-worker ASGI server and event loop that accepts the socket and drives the FastAPI app. | `src/switchyard_gateway/bootstrap.py:98` |
| `CHAT_ROUTE` | 2 | intermediary | `create_app.chat` | FastAPI route handler for POST /v1/chat/completions; owns authentication, body cap, validation, forwarding handoff, the JSON rewrite, and error responses. | `src/switchyard_gateway/adapters/ingress.py:249` |
| `AUTHORIZER` | 2 | intermediary | `create_app.authorize` | Constant-time Bearer API-key check; raises `unauthorized` on mismatch. | `src/switchyard_gateway/adapters/ingress.py:195` |
| `VALIDATOR` | 2 | intermediary | `_validate` | Validates model, messages, roles, text content, tool calls, and tool results into a `Payload`. | `src/switchyard_gateway/adapters/ingress.py:45` |
| `GATEWAY_OPEN` | 3 | intermediary | `Gateway.open` | Application orchestration: routes the alias, protects history, compresses, selects a replica, and opens exactly one upstream exchange. | `src/switchyard_gateway/application.py:125` |
| `ELIGIBLE_HISTORY` | 3 | intermediary | `eligible_indices` | Selects compressible older history while protecting instructions, cached rows, and complete live tool exchanges. | `src/switchyard_gateway/application.py:27` |
| `ROUTER` | 4 | intermediary | `SwitchyardRouter.route` | Tier router adapter; normalizes messages, invokes the stage algorithm, and restores original messages. | `src/switchyard_gateway/adapters/switchyard.py:88` |
| `SWITCHYARD_SDK` | 5 | intermediary | `algorithms.stage_router` | Pinned `switchyard.libsy` stage router invoked with capture clients; its synthetic completion never reaches a backend or client. | `src/switchyard_gateway/adapters/switchyard.py:99` |
| `COMPRESSOR` | 4 | intermediary | `HeadroomCompressor.compress` | Compression adapter that serializes access to Headroom's shared pipeline through a bounded slot semaphore. | `src/switchyard_gateway/adapters/headroom.py:50` |
| `THREAD_POOL` | 7 | intermediary | `ThreadPoolExecutor` | Single compression execution thread; cancellation does not stop a running thread and the slot is released only when it exits. | `src/switchyard_gateway/adapters/headroom.py:18` |
| `HEADROOM_SDK` | 5 | intermediary | `compress` | Pinned `headroom-ai` synchronous compressor; ML compression disabled and failures returned as `failed_unknown`. | `src/switchyard_gateway/adapters/headroom.py:25` |
| `TRANSPORT` | 4 | intermediary | `HttpxTransport.send` | HTTPX-backed transport adapter; opens one upstream request with normalized failures and no hidden retries. | `src/switchyard_gateway/adapters/httpx.py:46` |
| `BACKEND` | 6 | intermediary | — | External OpenAI-compatible upstream that produces the JSON completion. | external upstream |
| `HTTPX_RESPONSE` | 4 | intermediary | `HttpxResponse` | Open upstream response adapter exposing status, headers, chunk iteration, and idempotent close. | `src/switchyard_gateway/adapters/httpx.py:11` |
| `GATEWAY_BODY` | 3 | intermediary | `Gateway.body` | Reads upstream bytes to exhaustion and records `first_body_byte_ms` on the first non-empty chunk. | `src/switchyard_gateway/application.py:251` |
| `GATEWAY_FINISH` | 3 | intermediary | `Gateway.finish` | Closes the upstream response and emits exactly one terminal request event with the recorded outcome. | `src/switchyard_gateway/application.py:260` |
| `EVENTS` | 4 | sink | `JsonEvents.emit` | Sanitized JSON event sink writing one record per line to stdout. | `src/switchyard_gateway/adapters/logging.py:18` |

## Sequence

1. (seq 1–2) An authenticated client posts a non-streaming Chat Completions payload; the single Uvicorn worker dispatches the ASGI scope to `create_app.chat` (`ingress.py:249`).
2. (seq 3–4) `chat` calls `authorize` (`ingress.py:195`), which compares `Authorization` to `Bearer {gateway.settings.api_key}` with `hmac.compare_digest` (`ingress.py:196-199`); the check returns cleanly.
3. (seq 5–6) `chat` accumulates `request.stream()` into a `bytearray`, enforcing `gateway.settings.max_request_bytes` (`ingress.py:258-262`), then `json.loads(..., parse_constant=_reject_constant)` (`ingress.py:41`) and `_validate` (`ingress.py:45`) return a validated `Payload`. `opened` becomes `True` (`ingress.py:267`).
4. (seq 7–11) `chat` calls `Gateway.open` (`application.py:125`). Inside `_open` (`application.py:141`) a pair alias resolves and `SwitchyardRouter.route` (`switchyard.py:88`) normalizes messages and runs `algorithms.stage_router` with `_CaptureClient`s (`switchyard.py:63`). Exactly one capture client records one request; the synthetic completion is discarded, original messages are restored, and a `RoutingResult` is returned (`switchyard.py:104-127`). A direct model label instead skips routing with `tier="direct"` (`application.py:157-159`).
5. (seq 12–13) `eligible_indices` (`application.py:27`) returns the compressible older-history indices, retaining system/developer rows, cache-marked rows, the latest user/assistant turn, and connected live tool exchanges.
6. (seq 14–19) `HeadroomCompressor.compress` (`headroom.py:50`) submits `_compress` to the single `ThreadPoolExecutor` (`headroom.py:18`), which calls `headroom.compress` with `kompress_model="disabled"` and returns a `CompressionResult` (`savings`, `no_savings`, or `failed_unknown`). `_open` rejects any result whose outcome is unknown or whose message structure changed (`_same_structure`, `application.py:58`), then writes the compressed rows back into the private `prepared` copy (`application.py:172-188`).
7. (seq 20–24) `Gateway._select` round-robins an endpoint and `HttpxTransport.send` (`httpx.py:46`) opens `POST {base_url}/chat/completions` with `json=request`, returning an `HttpxResponse`. `_open` returns an `Exchange` carrying the open response, the event record, and start timings (`application.py:247-248`).
8. (seq 25–30) `chat` reads the response to exhaustion through `async for chunk in gateway.body(exchange)` (`ingress.py:285`), which pulls `HttpxResponse.chunks` (`httpx.py:27`) and records `first_body_byte_ms` on the first non-empty chunk (`application.py:253-258`).
9. (seq 31–35) `chat`'s `finally` calls `gateway.finish(exchange, "completed")` inside `anyio.CancelScope(shield=True)` (`ingress.py:333-335`) because `handed_off` is `False`. `finish` marks the exchange finished, closes the upstream response, updates `outcome`/`total_ms`, and emits exactly one terminal event through `JsonEvents.emit` (`application.py:260-271`).
10. (seq 36–37) `chat` rewrites `value["model"] = payload["model"]` (`ingress.py:295`), captures recognized integer `usage` fields into `exchange.event["usage"]` (`_usage`, `ingress.py:28`) and returns `JSONResponse(value, headers=response_headers)` with `x-request-id`, `x-gateway-model`, and `x-gateway-endpoint` (`ingress.py:269-273`, `284`). Uvicorn writes the 200 JSON body to the client.

## Error paths

### Routing adapter failure

_Covers:_ seq 45, 46, 47, 58

`SwitchyardRouter.route` raises `invalid_routing_request` (400) for a malformed routing request, `routing_failed` (502) for any other SDK escape, and `unsupported_routing_rewrite` when messages cannot be mapped back verbatim (`switchyard.py:95-96`, `124-131`). `_open` additionally rejects a tier outside `capable`/`efficient` with `invalid_routing_result` (`application.py:152-153`). `Gateway.open`'s `except BaseException` (`application.py:131-139`) records `outcome="failed"`, the status, the sanitized code, and `total_ms`, emits the terminal-failure event (seq 58), and re-raises; `chat` returns the sanitized body without a second event because `opened` is already `True`.

### Unauthorized

_Covers:_ seq 50, 51, 52, 56

`authorize` (`ingress.py:195`) raises `GatewayError("unauthorized", 401)` when the Bearer header does not match. `chat`'s `except GatewayError` (`ingress.py:301`) builds `_error` (`ingress.py:105`), a JSONResponse with the sanitized code and `x-request-id`. Because the rejection happens before `opened = True` and `exchange` stays `None`, `chat` emits a `{"event": "request", "outcome": "rejected"}` record (seq 56, `ingress.py:304-313`).

### Invalid or oversized request

_Covers:_ seq 53, 54, 55, 56

`_validate` (`ingress.py:45`) raises 400-coded `GatewayError`s for a missing/non-string `model`, non-boolean `stream`, empty or malformed `messages`, invalid roles, unsupported content, and invalid tool calls/results. The body loop raises `request_too_large` (413, `ingress.py:261-262`) and the `json.loads` wrapper raises `invalid_json` (400, `ingress.py:263-266`). Since `opened` is still `False`, the rejected request event (seq 56) is emitted and `_error` returns the sanitized body.

### Unknown model

_Covers:_ seq 57, 58, 59, 60

When the alias is neither a pair nor a configured model, `_open` raises `GatewayError("unknown_model", 404)` (`application.py:161`). `Gateway.open`'s `except BaseException` records `outcome="failed"`, `status=404`, and the sanitized code, emits the event (seq 58), and re-raises. `chat` returns the 404 body (seq 59) without emitting a second event because `opened` is already `True`.

### Upstream connect, timeout, or failover exhaustion

_Covers:_ seq 61, 62, 63, 64, 65, 66, 67

`HttpxTransport.send` (`httpx.py:53-66`) translates `httpx.ConnectError`/`ConnectTimeout` into `ConnectFailure`, other timeouts into `upstream_timeout` (504), and remaining transport errors into `upstream_transport_failed`. In `_open` (`application.py:195-249`), a `ConnectFailure` or retryable 429/502/503/504 cools the endpoint down (`_cooldown`, `application.py:111`), emits a `replica_failure` record (seq 62), and permits exactly one attempt on a different eligible replica, announced by a `retry` record (seq 64). A retryable response is closed before failover (seq 63). A second failure raises the underlying `ConnectFailure`, and `_select` raises `no_available_replica` (503) when no replica is eligible (`application.py:109`); `Gateway.open` emits the failure event and `chat` returns the sanitized status. The selected tier never changes.

### Upstream rejection after open

_Covers:_ seq 68, 69, 70, 71, 72, 73, 74

After `open` returns, `chat` checks `exchange.response.status >= 400` and raises `upstream_rejected_request` with that status (`ingress.py:274-276`). This covers non-retryable statuses and a retryable status when no alternate replica exists (the single-replica case pinned by `tests/test_application.py:test_single_replica_keeps_rejected_response`). The upstream body is never forwarded. `chat`'s `finally` then calls `finish(exchange, "failed")` because `handed_off` is `False`, closing the upstream and emitting the terminal `outcome="failed"` event (seq 73, 74).

### Compression fallback

_Covers:_ seq 75, 76

`HeadroomCompressor._compress` catches every exception and returns `failed_unknown` when Headroom raises or reports zero `tokens_before`/growing tokens (`headroom.py:36-48`). `_open` catches any `Exception` around `compress` — including `invalid_compression_result` and `compression_structure_changed` (`application.py:176-179`) — and continues with the uncompressed private copy, recording `compression="failed_unknown"` (`application.py:189-190`). This is deliberate fail-open: the request still succeeds, so the client sees no error, but the terminal event distinguishes a proven no-savings result from an unknown one.

### Upstream content type or body shape failure

_Covers:_ seq 83, 84, 77, 78, 79, 73, 74

Before buffering a 200, `chat` reads `exchange.response.headers["content-type"]` (seq 83) and raises `invalid_upstream_content_type` when it does not contain `json` (seq 84, `ingress.py:282-283`), mirroring the streaming branch's `text/event-stream` guard. The body is never read on that branch. `HttpxResponse.chunks` translates `httpx.HTTPError` into `GatewayError("upstream_read_failed")` (`httpx.py:32-33`). While buffering, `chat` raises `upstream_response_too_large` past 32 MiB (`ingress.py:284-288`) and `invalid_upstream_response` when the body is not a JSON object with `choices` and no `error` (`ingress.py:289-294`). Each is a `GatewayError`, so `chat` returns the 502 body (seq 78, 79) and `finish` closes the upstream and emits the failed terminal event (seq 73, 74).

### Unexpected server failure

_Covers:_ seq 80, 81, 82, 73, 74

Any non-`GatewayError` exception is caught by `chat`'s `except Exception` (`ingress.py:318-331`), which records `status=500, error="request_failed"` on the exchange (or emits a failed request event when no exchange exists, seq 82) and returns a sanitized 500 body. The exception text is never exposed. When an exchange exists the terminal event comes from the `finally` `finish` (seq 73, 74).

## Anomalies

### Unguarded terminal close can discard an already-completed response

_Covers:_ seq 85, 86, 87

`Gateway.finish` closes the upstream inside a `try` whose `finally` always emits the terminal event (`application.py:265-270`). If `HttpxResponse.close` (`httpx.py:35-37`, delegating to `httpx.Response.aclose`) raises — plausible after a body read that already failed with `upstream_read_failed` — the event is emitted with `outcome="completed"` (seq 86) and then the exception propagates out of `finish` into `chat`'s `finally` (`ingress.py:332-335`). Because that `finally` runs while unwinding a successful `return JSONResponse(...)`, the raised exception replaces the return value, so the client receives an ASGI server error instead of the completion that was already built and logged as completed. The non-streaming path has no shield or fallback around the close itself; only the caller is shielded.

### Replica cooldown and round-robin state dies on restart

_Covers:_ seq 90

`Gateway.__init__` keeps `_positions` and `_cooldowns` as plain in-memory dicts (`application.py:86-87`). They are process-local by design and explicitly single-worker (README §Configuration), so a Uvicorn restart recreates the `Gateway` and forgets every cooldown and rotation position. A backend that was cooled down for an outage is immediately eligible again after restart, and round-robin restarts at the first endpoint; the state is only ever rebuilt by observation. `tests/test_application.py:test_fresh_cooldown_table_keeps_replicas_eligible` documents that a fresh table keeps all replicas eligible. The same state also backs `/health/readiness` (`application.py:89-96`), so readiness forgets cooldowns on restart along with routing; see [health-check](health-check.md).

## Verification

- `src/switchyard_gateway/adapters/ingress.py:create_app.chat` — traced authorization, the `max_request_bytes` body cap, `_reject_constant`, `json.loads`/`_validate`, `gateway.open`, the `status >= 400` gate, the non-streaming `content-type` JSON guard (`invalid_upstream_content_type`), the buffer loop with the 32 MiB `_MAX_RESPONSE` bound, the `model` rewrite, `_usage` capture, `JSONResponse`, `except GatewayError`/`CancelledError`/`Exception`, and the `finally` finish guard.
- `src/switchyard_gateway/adapters/ingress.py:create_app.authorize` and `_validate`/`_usage`/`_error` — confirmed the constant-time check, every 400 code, the `invalid_json`/`request_too_large` codes, the usage allowlist, and the sanitized error body.
- `src/switchyard_gateway/application.py:Gateway.open` / `_open` / `_select` / `_cooldown` — confirmed routing, `eligible_indices`, structure-checked compression fail-open, round-robin, bounded two-attempt failover, retryable-status cooldowns, and the `BaseException` event emit/re-raise.
- `src/switchyard_gateway/application.py:Gateway.body` / `finish` and `eligible_indices`/`_same_structure` — confirmed first-body-byte timing, the idempotent `finished` guard, close-before-emit ordering, and protected-row selection.
- `src/switchyard_gateway/adapters/switchyard.py:SwitchyardRouter.route` / `_normalize` / `_CaptureClient` — confirmed capture-client stage routing and that exactly one request is captured.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor.compress` / `_compress` — confirmed executor isolation, fail-open `failed_unknown`, and slot release on thread exit.
- `src/switchyard_gateway/adapters/httpx.py:HttpxTransport.send` / `HttpxResponse.chunks` / `close` — confirmed the JSON POST, header auth, normalized transport failures, and idempotent close.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents.emit` — confirmed one sanitized JSON line per terminal event.
- `src/switchyard_gateway/bootstrap.py:build_app` / `main` / `uvicorn.run` — confirmed single-worker composition and process ownership.
- `src/switchyard_gateway/domain.py:Settings` / `GatewayError` / `ConnectFailure` / `CompressionResult` and `src/switchyard_gateway/ports.py:TierRouter`/`HistoryCompressor`/`BackendTransport`/`EventSink` — confirmed the boundary types the paths cross.
- `tests/test_ingress.py:TestChatIngress` — pins alias/usage rewriting, `private output` exclusion from events, invalid-request 400s, the upstream-body non-exposure, the `text/html` 200 surfaced as `invalid_upstream_content_type` with the upstream closed, and invalid upstream JSON closing the response.
- `tests/test_application.py:TestReplicaSelection` / `TestReplicaPolicyContracts` — pin round-robin, retryable-status failover, cooldown expiry, no-eligible-replica 503, compression fail-open, and structure-mutation rejection.
- `tests/test_events.py` — pins the completed/failed event contracts and the `retry`/`replica_failure` records.
- `docs/dataflow/data-flow-graph.schema.json` — contract revision 3 validated by `jsonschema` and `check_graphs.py`.

## Serialization notes

### Roles

- `CLIENT` is the only `initiator` (initiator-wins): it starts the cycle and receives the JSON return, so its termination is derived from incoming return edges rather than declared.
- `EVENTS` is the only `sink`, the terminal side effect with no outgoing edges; `BACKEND` is an `intermediary` because it returns a response rather than terminating.
- No `dead-end` node: every failure path is a handled error surfaced to the caller, and the anomalies below are modeled as `outcome: "anomaly"` links rather than a distinct termination node.

### Seq ordering

- Happy-path steps run 1–37 in lifecycle order; the routing-failure branch occupies 45–47, handled errors 50–84, and anomalies 85–90. Gaps keep the subgraphs separable inside one total order and no seq value is duplicated.
- `finish` (seq 31–35) precedes the response handoff (seq 36–37) because `chat`'s `finally` runs while unwinding the `return JSONResponse(...)` expression, so the terminal event is emitted before the bytes are written to the client.

### Kind mapping

- Request/response pairs use `call`/`return`; terminal and replica observability lines use `event`; the design-level restart observation (seq 90) uses `note`.
- The retryable-response close before failover (seq 63) is a `call` even though it sits on the error subgraph, because it is a real synchronous call to `HttpxResponse.close`.
- The escaping-cleanup fault (seq 87) is an `event` because the exception leaves the request handler rather than returning a value.

### Modeling choices

- The upstream exchange is opened once, so `TRANSPORT`/`BACKEND`/`HTTPX_RESPONSE` are shared by the happy and error subgraphs; the retryable branch is expressed as additional error links (seq 61–65) instead of duplicating the open sequence.
- Compression success and fail-open share the same adapter nodes; the fallback is a single error link (`COMPRESSOR → GATEWAY_OPEN`, seq 76) plus the inner SDK swallow (seq 75) rather than re-listing the executor path.
- All pre-open handled errors originate at `CHAT_ROUTE` because that is where `_error` builds the JSONResponse; the raise site is captured by the preceding `outcome: error` return into `CHAT_ROUTE`.

## References

### Specs and decisions

- [README.md](../../README.md) §Configuration — cooldowns, retry rules, and single-worker constraints on replica state.
- [README.md](../../README.md) §Client interface — non-streaming responses, alias preservation, and the `x-gateway-*` header contract.
- [README.md](../../README.md) §Observability — request event fields and the completed/failed outcomes.
- [AGENTS.md](../../AGENTS.md) §Contracts — one target call, routing-before-compression, and sanitized logs.

### Related lifecycles

- [chat-stream](chat-stream.md) — the SSE sibling: same ingress, routing, compression, replica policy, and terminal cleanup, but hands the response off before streaming instead of buffering it.
- [model-discovery](model-discovery.md) — shares `authorize`, `_error`, and the event sink on a different route.

### Code

- `src/switchyard_gateway/adapters/ingress.py:create_app.chat` — validation, upstream status gate, body buffering, JSON rewrite, and pre-stream error surfacing.
- `src/switchyard_gateway/adapters/ingress.py:_validate` / `_usage` / `_error` — request shape checks, usage allowlist, and sanitized error body.
- `src/switchyard_gateway/application.py:Gateway` — routing, compression, replica selection, body, and finish orchestration.
- `src/switchyard_gateway/adapters/httpx.py:HttpxResponse` — buffered upstream bytes and close.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents.emit` — terminal event serialization.
