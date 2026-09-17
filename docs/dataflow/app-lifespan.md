<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# app-lifespan. Application Lifespan Construction and Shutdown

`build_app` composes an app but allocates no adapter resource: the FastAPI
lifespan owns them. On startup the lifespan constructs the pooled HTTP client,
the Headroom compressor (one execution thread), the Switchyard router, the HTTPX
transport, and the Gateway, registering each close on an `AsyncExitStack`, and
emits one startup event. At termination it releases every step independently — a
close failure is recorded but cannot skip the next step — and emits exactly one
sanitized shutdown event after release.

- **Entry point:** the `lifespan` async context manager defined inside
  `src/switchyard_gateway/bootstrap.py:build_app` (`bootstrap.py:28-68`)
- **Trigger:** the operator runs the CLI (`switchyard_gateway.bootstrap:main`),
  which loads configuration, calls `build_app`, and passes the app to
  `uvicorn.run(..., workers=1)`.
- **Termination:** sink at `STDOUT` for the startup and shutdown event branches
  (seq 28, 40, 47); the lifecycle returns to the initiator `OPERATOR` when
  `uvicorn.run` returns (seq 44).
- **Response:**
  - **Success:** the ASGI server starts serving after the lifespan yields
    (seq 31); on shutdown the executor drains, the pool closes, exactly one
    `shutdown` record is emitted (seq 33-42), and the process exits.
  - **Failure:** a configuration error is emitted as a sanitized
    `startup_failed` event and the process exits via `SystemExit(1)` before the
    server binds (seq 45-48); a failing startup emit still releases every
    adapter and still emits `shutdown` before propagating (seq 49).
  - **Exception:** a failing `client.aclose()` or `compressor.close()` is
    suppressed and recorded, the other release still runs, and the terminal
    record carries `"error": "shutdown_failed"` (seq 50-51).

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `OPERATOR` | 1 | initiator | `main` | CLI entry: loads config, builds the app, starts one uvicorn worker. | `src/switchyard_gateway/bootstrap.py` |
| `UVICORN_SERVER` | 7 | intermediary | `uvicorn.run` | Single-process ASGI server and event loop; drives lifespan start/stop. | `src/switchyard_gateway/bootstrap.py` |
| `CONFIG` | 4 | intermediary | `load_config` | JSONC adapter resolving env references into validated `Settings`. | `src/switchyard_gateway/adapters/config.py` |
| `BUILD_APP` | 4 | intermediary | `build_app` | Composition root: creates the event sink and the app carrying the lifespan; allocates no adapter. | `src/switchyard_gateway/bootstrap.py` |
| `GATEWAY` | 3 | intermediary | `Gateway` | Application core holding injected ports plus process-local routing state; installed on `app.state`. | `src/switchyard_gateway/application.py` |
| `HTTP_CLIENT` | 4 | intermediary | `httpx.AsyncClient` | Pooled async client constructed in the lifespan and released by `aclose()`. | `src/switchyard_gateway/bootstrap.py` |
| `COMPRESSOR` | 4 | intermediary | `HeadroomCompressor` | Headroom adapter owning one execution thread and a capacity semaphore. | `src/switchyard_gateway/adapters/headroom.py` |
| `COMPRESSION_EXECUTOR` | 7 | intermediary | `ThreadPoolExecutor` | The compression worker thread, joined by `shutdown(wait=True)`. | `src/switchyard_gateway/adapters/headroom.py` |
| `EVENTS` | 4 | intermediary | `JsonEvents` | JSON-lines sink writing sanitized gateway records to stdout. | `src/switchyard_gateway/adapters/logging.py` |
| `STDOUT` | 7 | sink | `sys.stdout` | Process stdout receiving the startup and shutdown records. | `src/switchyard_gateway/adapters/logging.py` |
| `ROUTER` | 4 | intermediary | `SwitchyardRouter` | Switchyard stage-routing adapter; construction only stores policy. | `src/switchyard_gateway/adapters/switchyard.py` |
| `TRANSPORT` | 4 | intermediary | `HttpxTransport` | Backend transport wrapping the injected pooled client. | `src/switchyard_gateway/adapters/httpx.py` |
| `CREATE_APP` | 2 | intermediary | `create_app` | Ingress factory building the FastAPI app, recording the lifespan, and resolving the Gateway from `app.state`. | `src/switchyard_gateway/adapters/ingress.py` |
| `FASTAPI_APP` | 2 | intermediary | `FastAPI` | ASGI app that enters and resumes the lifespan context manager. | `src/switchyard_gateway/adapters/ingress.py` |
| `LIFESPAN` | 4 | intermediary | `lifespan` | Context manager constructing adapters on an `AsyncExitStack`, emitting startup/shutdown. | `src/switchyard_gateway/bootstrap.py` |

Group legend: 1=Client, 2=Ingress, 3=Application core, 4=Gateway adapter, 7=Process/OS.

## Sequence

1. (seq 1-2) `main` (`bootstrap.py:main`) calls `load_config(args.config)`
   (`adapters/config.py:load_config`) and receives a validated `Settings` carrying
   model/pair labels and `compression_workers`.
2. (seq 3-10) `main` calls `build_app(settings)` (`bootstrap.py:99`).
   `build_app` constructs `JsonEvents()` bound to `sys.stdout`
   (`bootstrap.py:26`; `adapters/logging.py:15-16`) and calls
   `create_app(lifespan=lifespan)` (`bootstrap.py:70`), which builds
   `FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)`
   (`adapters/ingress.py:180`) and returns the app. No client, executor, router,
   transport, or Gateway exists yet when `build_app` returns.
3. (seq 11) `main` calls
   `uvicorn.run(app, host, port, workers=1, access_log=False, log_config=None)`
   (`bootstrap.py:98-105`).
4. (seq 12-13) Uvicorn drives ASGI lifespan startup and enters the `lifespan`
   context manager (`bootstrap.py:28-29`).
5. (seq 14-15) `lifespan` constructs the shared
   `httpx.AsyncClient(timeout=..., follow_redirects=False, trust_env=False)`
   (`bootstrap.py:41-45`) with explicit timeouts, no redirects, and no ambient
   proxy/env trust, and registers `client.aclose` with
   `stack.push_async_callback(release, client.aclose)` (`bootstrap.py:46`).
6. (seq 16-19) `lifespan` constructs
   `HeadroomCompressor(settings.compression_workers)` (`bootstrap.py:47`); its
   `__init__` creates
   `ThreadPoolExecutor(max_workers=1, thread_name_prefix="compression")` and a
   `Semaphore(workers)` that bounds running-plus-queued work
   (`adapters/headroom.py:15-19`). `compressor.close` is registered on the same
   stack (`bootstrap.py:48`).
7. (seq 20-25) `lifespan` constructs `SwitchyardRouter(settings.stage)` — which
   only stores the `StagePolicy`, making no SDK call (`adapters/switchyard.py:85-86`) —
   `HttpxTransport(client)`, which only stores the client
   (`adapters/httpx.py:43-44`), and
   `Gateway(settings, router, compressor, transport, events)`
   (`bootstrap.py:49-55`; `application.py:68-87`). The Gateway starts with empty
   `_positions` and `_cooldowns` dictionaries.
8. (seq 26) `lifespan` assigns `app.state.gateway = gateway`; ingress handlers
   resolve the live gateway per request (`bootstrap.py:49`; `ingress.py:181-186`).
9. (seq 27-30) `lifespan` calls
   `events.emit({"event": "startup", "models": len(settings.models), "pairs": len(settings.pairs)})`
   (`bootstrap.py:56-62`). `JsonEvents.emit` prepends a UTC timestamp, serializes
   one compact JSON line, and flushes it to `STDOUT` (`adapters/logging.py:18-22`).
10. (seq 31) `lifespan` yields (`bootstrap.py:63`); startup completes and the
    server begins serving.
11. (seq 32) On shutdown the generator is resumed at the `yield`
    (`bootstrap.py:63`).
12. (seq 33-36) The `AsyncExitStack` unwinds last-in-first-out; the first
    `release` awaits `compressor.close()` (`bootstrap.py:48, 62`), which runs
    `asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)`:
    in-flight compression finishes and queued submissions are cancelled
    (`adapters/headroom.py:64-66`).
13. (seq 37-38) The next `release` awaits `client.aclose()`, releasing the pooled
    connections (`bootstrap.py:46`).
14. (seq 39-42) The `finally` emits exactly one
    `{"event": "shutdown"}` record after every release has been attempted,
    adding `"error": "shutdown_failed"` when any release raised
    (`bootstrap.py:64-68`).
15. (seq 43-44) The lifespan exits; `uvicorn.run` returns to `main` and the
    process falls through to exit.

## Error paths

### Startup configuration failure

_Covers:_ seq 45, `CONFIG` → `OPERATOR` (outcome: error); seq 46, `OPERATOR` → `EVENTS` (outcome: error); seq 47, `EVENTS` → `STDOUT` (outcome: error); seq 48, `OPERATOR` → `UVICORN_SERVER` (outcome: error)

`load_config` wraps missing/empty environment references
(`adapters/config.py:176-186`) and any parse/validation failure
(`adapters/config.py:189-197`) in `GatewayError("invalid_configuration", 500)` or
`GatewayError("missing_configuration_environment", 500)`. `main` also raises
`GatewayError("litellm_must_not_be_installed", 500)` when the forbidden package is
importable (`bootstrap.py:89-90`). The handler at `bootstrap.py:92-94` emits
`{"event": "startup_failed", "error": error.code}` — only the sanitized code — and
raises `SystemExit(1)`, so `uvicorn.run` is never reached, no port is bound, and
no adapter resource has been allocated.

### A failing startup emit still releases every adapter

_Covers:_ seq 49, `LIFESPAN` → `EVENTS` (outcome: error)

The startup `events.emit(...)` sits inside the guarded `try` and the
`AsyncExitStack` (`bootstrap.py:39-62`). `JsonEvents.emit` performs a write and a
flush on stdout (`adapters/logging.py:18-22`), both of which can raise (broken
pipe, disk full, closed stream). If it raises, the stack still unwinds: the
compressor is drained, the client pool is closed, and the `finally` still emits
the terminal shutdown record before the exception propagates to the server
(seq 49). Guarded by
`tests/test_bootstrap.py:test_startup_emit_failure_still_releases_adapters` and
`tests/test_bootstrap_contract.py:test_startup_emit_failure_releases_adapters_and_reports_shutdown`.

### A failed release cannot skip another release

_Covers:_ seq 50, `LIFESPAN` → `HTTP_CLIENT` (outcome: error); seq 51, `LIFESPAN` → `COMPRESSOR` (outcome: error)

Each close is wrapped by `release` (`bootstrap.py:32-37`), which records the
failure and lets the `AsyncExitStack` continue, so neither
`client.aclose()` raising (seq 50) nor `compressor.close()` raising (seq 51)
skips the other step. The terminal record reports the sanitized outcome as
`"error": "shutdown_failed"`. Guarded by
`tests/test_bootstrap.py:test_client_close_failure_does_not_skip_the_compressor`,
`tests/test_bootstrap.py:test_compressor_close_failure_does_not_skip_the_client`,
`tests/test_bootstrap_contract.py:test_client_close_failure_does_not_skip_the_compressor`,
and
`tests/test_bootstrap_contract.py:test_compressor_close_failure_does_not_skip_the_client`.

## Anomalies

Resolved by the lifespan-ownership change; the graph now carries no anomaly
links, and the four previously recorded findings are pinned by tests:

- **Resolved: startup emit sat outside the shutdown guard.** The emit is inside
  the guarded `try` (`bootstrap.py:39-62`), so a raising sink cannot skip
  cleanup; the behavior is the error path seq 49.
- **Resolved: sequential cleanup skipped the compressor when the client close
  raised.** Releases are independent `AsyncExitStack` callbacks wrapped in
  `release` (`bootstrap.py:32-37, 46-48`); the behavior is the error paths
  seq 50-51.
- **Resolved: resources were constructed before the lifespan owned them.**
  `build_app` allocates only the sink and the app (`bootstrap.py:26-70`); the
  client and executor are constructed inside the lifespan, so a bind/start
  failure before lifespan entry leaves nothing to leak (seq 52). Guarded by
  `tests/test_bootstrap.py:test_build_app_defers_adapter_construction_until_lifespan`
  and `tests/test_bootstrap.py:test_main_start_failure_constructs_no_adapters`.
- **Resolved: orderly shutdown was unobservable.** Exactly one
  `{"event": "shutdown"}` is emitted after release (`bootstrap.py:64-68`),
  guarded by `test_clean_shutdown_emits_one_terminal_event` and the Hypothesis
  property
  `tests/test_bootstrap.py:test_shutdown_is_reported_exactly_once_under_fault_injection`
  over startup-emit, client-close, and compressor-close failures.

## Verification

- `src/switchyard_gateway/bootstrap.py:build_app` — confirmed it creates only
  `JsonEvents` and calls `create_app(lifespan=lifespan)`; no client, executor,
  router, transport, or Gateway is constructed before the lifespan runs.
- `src/switchyard_gateway/bootstrap.py:lifespan` — confirmed adapters are
  constructed inside the `try` and registered with
  `stack.push_async_callback(release, ...)`, that `release` records failures
  without re-raising, and that the `finally` emits one shutdown record with a
  sanitized `shutdown_failed` code.
- `src/switchyard_gateway/bootstrap.py:main` — confirmed config load precedes
  `build_app`, that `uvicorn.run(..., workers=1)` starts the app, and that
  config failure emits `startup_failed` and raises `SystemExit(1)`.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — confirmed the lazy
  `lifespan` parameter is passed to `FastAPI(lifespan=lifespan)`, that `current`
  resolves the gateway from `request.app.state.gateway`, and that an explicitly
  supplied gateway is installed at construction.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor.close` —
  confirmed `shutdown(wait=True, cancel_futures=True)` is offloaded with
  `asyncio.to_thread`.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents.emit` — confirmed one
  JSON line plus flush per record, timestamped, with no raw exception
  serialization.
- `tests/test_bootstrap.py` (unit) — fake-port coverage of construction
  deferral, teardown order, event payloads, close-fault isolation, CLI
  arguments, and the Hypothesis shutdown-once property.
- `tests/test_bootstrap_contract.py` (integration) — drives the real
  `build_app` lifespan through the ASGI lifespan protocol with the real
  `httpx.AsyncClient`, `HeadroomCompressor`, `JsonEvents`, `SwitchyardRouter`,
  and `HttpxTransport`, fault-injecting stdout writes and close failures.
- `tests/conftest.py:LifespanDriver` — sends `lifespan.startup` /
  `lifespan.shutdown` ASGI messages and records `startup.failed`/`shutdown.failed`
  outcomes, so no tier starts a real server.
- `docs/dataflow/README.md` — the CLI-startup pair documents the argparse
  `startup_failed` path; the construction-before-ownership anomaly digest entry
  is removed because no adapter predates its lifespan owner.

## Serialization notes

### Group assignment for the composition root

- The legend lists "JSONC config" and "JSON events" under group 4 (Gateway
  adapter), so `CONFIG`, `EVENTS`, `BUILD_APP`, and `LIFESPAN` are group 4 even
  though `build_app`/`lifespan` are composition, not an adapter. `UVICORN_SERVER`,
  `COMPRESSION_EXECUTOR`, and `STDOUT` are group 7 (Process/OS); `GATEWAY` is
  group 3; `CREATE_APP`/`FASTAPI_APP` are group 2.

### Role assignments

- `OPERATOR` is `initiator` only under the initiator-wins convention: it starts
  the process and receives the final return (seq 44); its termination is derived
  from that incoming edge, not declared.
- `STDOUT` is `sink`: the startup (seq 28), shutdown (seq 40), and failure
  (seq 47) event branches terminate there.
- No `dead-end` node was declared. Construction is deferred to the lifespan, so
  the previously absent interactions (seq 52) are now a success note rather than
  a premature termination.

### Seq ordering and kind mapping

- seq 1-10 are the composition path: `main` loads config, `build_app` creates
  the sink and the app, and no adapter exists yet.
- seq 11-44 are the runtime startup/serve/shutdown order: construction happens
  between lifespan entry (seq 13) and the startup emit (seq 27), and teardown
  drains compression (seq 33-36) before releasing the pool (seq 37-38) because
  `AsyncExitStack` unwinds LIFO.
- seq 45-51 are the handled and isolated failure paths, numbered after the happy
  path so outcome filters extract complete subgraphs; seq 52 is a success note
  recording the absent construction-before-ownership window.
- Constructor invocations are `call` and their results `return`; ASGI lifespan
  signals, the `app.state.gateway` installation, the startup/shutdown
  emissions, and the stdout writes are `event`; the ownership note is `note`.
- `multigraph` is `true` because `LIFESPAN → COMPRESSOR`,
  `LIFESPAN → HTTP_CLIENT`, and `LIFESPAN → EVENTS` each appear twice at
  different seq values (happy path plus failure path).
- The vendor SDKs `switchyard.libsy` (group 5) and `headroom` (group 5) are
  intentionally absent: neither is called during startup. `SwitchyardRouter` only
  stores a `StagePolicy`, and `HeadroomCompressor` only creates an executor and a
  semaphore; the module-level `from headroom import ...` executes at process
  import, before this lifecycle begins.

## References

### Specs and decisions

- [Data Flow Graph schema](data-flow-graph.schema.json) — contract stamped as
  `urn:data-flow-graph:schema:3` (root `version: 3`).
- [README](../../README.md) §Architecture and upgrades — "`bootstrap.py` wires
  their lifetimes"; ports are `TierRouter`, `HistoryCompressor`,
  `BackendTransport`, `EventSink`.
- [README](../../README.md) §Configuration — "Cooldowns and round-robin positions
  are process-local; run one worker", matching the empty `_positions`/`_cooldowns`
  created at construction.
- [AGENTS.md](../../AGENTS.md) — "Bootstrap owns adapter construction and
  shutdown."

### Related lifecycles

- [health-check](health-check.md) — shares `UVICORN_SERVER` and `FASTAPI_APP`; a
  probe only succeeds after this lifespan has bound and served.
- [cli-startup](cli-startup.md) — the operator/CLI path that calls `build_app` and
  enters this same `lifespan`; it reuses different node IDs for the same code.
- [chat-stream](chat-stream.md) — per-request traffic that depends on the pooled
  client and compression executor this lifespan creates and releases.

### Code

- `src/switchyard_gateway/bootstrap.py:build_app` — app assembly and lifespan owner.
- `src/switchyard_gateway/bootstrap.py:lifespan` — adapter construction, startup emit, independent release, terminal event.
- `src/switchyard_gateway/bootstrap.py:main` — config load, failure handling, uvicorn startup.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor` — executor and close.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents` — lifecycle records to stdout.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — FastAPI app, lifespan ownership, and `app.state` resolution.
- `src/switchyard_gateway/adapters/config.py:load_config` — Settings provenance and failure codes.
- `src/switchyard_gateway/application.py:Gateway` — application core and in-memory state.
