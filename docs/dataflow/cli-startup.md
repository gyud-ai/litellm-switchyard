<!-- pair-contract: urn:data-flow-graph:schema:3 -->

# cli-startup. CLI Startup and Worker Launch

The `switchyard-gateway` console script parses its arguments, refuses to run in a
LiteLLM-contaminated interpreter, loads and validates a JSONC configuration into
frozen `Settings`, and either exits after `--check` validation or wires the
adapters and starts a single Uvicorn worker that emits a `startup` event over the
ASGI lifespan.

- **Entry point:** `switchyard_gateway.bootstrap:main`, exposed as the
  `switchyard-gateway` console script in `pyproject.toml`
- **Trigger:** an operator runs the console script from a shell, a Compose
  service, or a container entrypoint
- **Termination:** `STDOUT` (sink) for the `--check` and `startup` records,
  `UVICORN` (sink) for the running worker, and `STDERR` (dead-end) for
  argument-parsing failures
- **Response:**
  - **Success:** `--check` prints `{"event":"configuration_valid"}` and exits 0;
    the server path prints `{"event":"startup",...}` and serves on `host:port`
  - **Failure:** `{"event":"startup_failed","error":"<sanitized_code>"}` on
    stdout followed by exit code 1
  - **Exception:** an argparse usage error exits 2 on stderr without a JSON
    record; an unhandled Uvicorn start failure propagates as a traceback after
    `build_app` has already allocated resources

## Participants

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `OPERATOR` | 1 | initiator | `switchyard-gateway` | Operator invoking the console script with --config and optionally --check. | `pyproject.toml` |
| `MAIN` | 3 | intermediary | `main` | Composition-root CLI entry point: parses arguments, guards LiteLLM, loads config, starts one Uvicorn worker. | `src/switchyard_gateway/bootstrap.py` |
| `ARG_PARSER` | 3 | intermediary | `argparse.ArgumentParser` | --config (Path, default config.jsonc) and --check argument parsing. | `src/switchyard_gateway/bootstrap.py` |
| `LITELLM_GUARD` | 3 | intermediary | `importlib.util.find_spec` | Preflight check that refuses an environment where litellm is importable. | `src/switchyard_gateway/bootstrap.py` |
| `BUILD_APP` | 3 | intermediary | `build_app` | Wires concrete adapters and returns the FastAPI app together with its lifespan. | `src/switchyard_gateway/bootstrap.py` |
| `GATEWAY` | 3 | intermediary | `Gateway` | Application orchestration core constructed from settings and injected ports. | `src/switchyard_gateway/application.py` |
| `LIFESPAN` | 3 | intermediary | `lifespan` | Async context manager emitting the startup event and closing client/compressor on shutdown. | `src/switchyard_gateway/bootstrap.py` |
| `INGRESS_APP` | 2 | intermediary | `create_app` | FastAPI ingress app factory that stores the lifespan and route handlers. | `src/switchyard_gateway/adapters/ingress.py` |
| `LOAD_CONFIG` | 4 | intermediary | `load_config` | JSONC parse, environment-reference resolution, and pydantic validation into Settings. | `src/switchyard_gateway/adapters/config.py` |
| `JSON_EVENTS` | 4 | intermediary | `JsonEvents` | One-JSON-record-per-line stdout event sink with UTC timestamps. | `src/switchyard_gateway/adapters/logging.py` |
| `SILENCE_LOGS` | 4 | intermediary | `silence_dependency_logs` | Disables dependency logging process-wide so only gateway JSON events are observable. | `src/switchyard_gateway/adapters/logging.py` |
| `HEADROOM` | 4 | intermediary | `HeadroomCompressor` | Compression adapter that eagerly allocates a single compression worker thread. | `src/switchyard_gateway/adapters/headroom.py` |
| `SWITCHYARD` | 4 | intermediary | `SwitchyardRouter` | Tier-routing adapter over the pinned Switchyard SDK. | `src/switchyard_gateway/adapters/switchyard.py` |
| `HTTPX_TRANSPORT` | 4 | intermediary | `HttpxTransport` | Single-attempt backend transport adapter over the pooled HTTPX client. | `src/switchyard_gateway/adapters/httpx.py` |
| `HTTPX_CLIENT` | 4 | intermediary | `httpx.AsyncClient` | Pooled HTTP client constructed in build_app and owned until lifespan shutdown. | `src/switchyard_gateway/bootstrap.py` |
| `SWITCHYARD_SDK` | 5 | intermediary | `switchyard.libsy.algorithms` | Pinned vendor tier-routing SDK imported by the Switchyard adapter. | `src/switchyard_gateway/adapters/switchyard.py` |
| `HEADROOM_SDK` | 5 | intermediary | `headroom.compress` | Pinned vendor structural-compression SDK imported by the Headroom adapter. | `src/switchyard_gateway/adapters/headroom.py` |
| `CONFIG_FILE` | 7 | intermediary | `config.jsonc` | JSONC configuration document read from disk. | `config.example.jsonc` |
| `ENV` | 7 | intermediary | `os.environ` | Process environment holding telemetry switches and JSONC-resolved secrets/backend URLs. | `src/switchyard_gateway/bootstrap.py` |
| `STDOUT` | 7 | sink | `sys.stdout` | JSON event stream consumed by the operator and container log collector. | `src/switchyard_gateway/adapters/logging.py` |
| `STDERR` | 7 | dead-end | `sys.stderr` | argparse usage output for malformed invocations, which bypasses the JSON event contract. | `src/switchyard_gateway/bootstrap.py` |
| `UVICORN` | 7 | sink | `uvicorn.run` | Single-worker ASGI server that terminates the startup path and owns the event loop. | `src/switchyard_gateway/bootstrap.py` |
| `THREAD_POOL` | 7 | intermediary | `ThreadPoolExecutor` | Compression execution thread owned by HeadroomCompressor, allocated at construction time. | `src/switchyard_gateway/adapters/headroom.py` |

## Sequence

1. (seq 1) `OPERATOR` invokes the `switchyard-gateway` console script
   (`pyproject.toml` `[project.scripts]` maps it to
   `switchyard_gateway.bootstrap:main`).
2. (seq 2, 3) `MAIN` builds an `argparse.ArgumentParser` with `--config`
   (`type=Path`, default `Path("config.jsonc")`) and `--check`
   (`action="store_true"`) and calls `parse_args()`
   (`bootstrap.py:main`). It receives back a `Namespace` with the parsed `Path`
   and boolean.
3. (seq 4) `MAIN` calls `silence_dependency_logs()`, which runs
   `logging.disable(logging.CRITICAL)` (`adapters/logging.py:silence_dependency_logs`).
4. (seq 5) `MAIN` writes three telemetry switches into the process environment:
   `HEADROOM_BEACON=off`, `DO_NOT_TRACK=1`, `HEADROOM_TELEMETRY=off`
   (`bootstrap.py:main`).
5. (seq 6, 7) Inside a `try`, `MAIN` calls `importlib.util.find_spec("litellm")`
   and requires it to return `None`; a non-`None` spec raises
   `GatewayError("litellm_must_not_be_installed", 500)`
   (`bootstrap.py:main`).
6. (seq 8–13) `MAIN` calls `load_config(args.config)`
   (`adapters/config.py:load_config`). `LOAD_CONFIG` reads the JSONC text via
   `Path.read_text()` (`CONFIG_FILE`), resolves complete-value `{"env": NAME}`
   references through `_resolve` against `os.environ` (`ENV`), rejects a missing
   or empty variable with `GatewayError("missing_configuration_environment", 500)`,
   and validates the document with `Config.model_validate(...).settings()`
   (pydantic models `StrictConfig`, `EndpointConfig`, `ModelConfig`,
   `PairConfig`, `StageConfig`, `CompressionConfig`, `ServerConfig`, `Config`).
   It returns a frozen `Settings`.
7. (seq 14–16) **Alternate terminal, only when `args.check` is set.** `MAIN`
   emits `{"event":"configuration_valid"}` through a fresh `JsonEvents`, which
   writes and flushes one JSON line to `STDOUT`, then returns without starting a
   server (`bootstrap.py:main`). These steps share a prefix with and never occur
   alongside the server path that follows.
8. (seq 17–32) On the server path, `MAIN` calls `build_app(settings)`
   (`bootstrap.py:build_app`). `BUILD_APP` constructs the pooled
   `httpx.AsyncClient` (`timeout`, `follow_redirects=False`, `trust_env=False`),
   the `HeadroomCompressor` (which eagerly creates its single-worker
   `ThreadPoolExecutor`), the `SwitchyardRouter`, and the `HttpxTransport`; it
   composes them into `Gateway(settings, router, compressor, transport, events)`
   and passes that plus the `lifespan` closure to `create_app`, receiving the
   `FastAPI` app.
9. (seq 33–38) `MAIN` calls `uvicorn.run(build_app(settings), host=...,
   port=..., workers=1, access_log=False, log_config=None)`
   (`bootstrap.py:main`). Uvicorn enters ASGI lifespan startup, which invokes
   `LIFESPAN`; `LIFESPAN` emits
   `{"event":"startup","models":N,"pairs":M}` through `JSON_EVENTS` to `STDOUT`,
   then yields and the server binds `settings.host:settings.port`, remaining
   open until shutdown (`bootstrap.py:lifespan`). The same `build_app` is the
   shared hinge between this CLI path and the app-lifespan path: `create_app`
   stores the lifespan it is handed (`adapters/ingress.py:create_app`).

## Error paths

### LiteLLM is importable

_Covers:_ seq 39, `LITELLM_GUARD` → `MAIN` (outcome: error)

`importlib.util.find_spec("litellm")` returning a spec means a LiteLLM install
contaminated the pinned environment. `main` raises
`GatewayError("litellm_must_not_be_installed", 500)` before any config is read
(`bootstrap.py:main`, lines 62–63).

### Configuration is invalid or an environment reference is missing

_Covers:_ seq 40, `LOAD_CONFIG` → `MAIN` (outcome: error)

`load_config` wraps `OSError`, `ValueError`, `TypeError`, and pydantic
`ValidationError` into `GatewayError("invalid_configuration", 500)` without
echoing values (`adapters/config.py:load_config`, line 196). `_resolve` raises
`GatewayError("missing_configuration_environment", 500)` when a referenced
variable is unset or empty (`adapters/config.py:_resolve`, line 181). Both
propagate out of the `try` block in `main`.

### Sanitized startup_failed and non-zero exit

_Covers:_ seq 41, `MAIN` → `JSON_EVENTS` (outcome: error); seq 42, `JSON_EVENTS` → `STDOUT` (outcome: error); seq 43, `MAIN` → `OPERATOR` (outcome: error)

The single `except GatewayError` in `main` receives every failure above, emits
`{"event":"startup_failed","error":error.code}` through a new `JsonEvents`, and
raises `SystemExit(1) from None` so no dependency traceback leaks
(`bootstrap.py:main`, lines 65–67). The operator sees one JSON record on stdout
and exit code 1; no `startup` event is emitted and no server binds.

## Anomalies

### Argument errors bypass the JSON event contract

_Covers:_ seq 44, `ARG_PARSER` → `STDERR` (outcome: anomaly)

`parser.parse_args()` runs before the `try`/`except GatewayError` block
(`bootstrap.py:main`, lines 56 and 61). An unknown flag or a malformed `--config`
value therefore makes argparse print usage to `STDERR` and exit 2
(`bootstrap.py:ARG_PARSER`), which is a present-but-off-contract path: the
lifecycle's advertised failure surface is a JSON `startup_failed` record, but
this class of operator error never produces one, and `STDERR` is never routed
through `JsonEvents`. An operator or log shipper parsing only stdout JSON will
see the process vanish with no gateway-owned terminal record.

### Global logging disable is never restored

_Covers:_ seq 45, `SILENCE_LOGS` → `MAIN` (outcome: anomaly)

`silence_dependency_logs` calls `logging.disable(logging.CRITICAL)` and provides
no counterpart to re-enable logging (`adapters/logging.py:silence_dependency_logs`,
lines 25–27). The side effect is process-global and permanent: any later library
that logs through the `logging` module — including Uvicorn/Starlette internals
that are not already suppressed by `log_config=None` — is muted for the life of
the process. In the CLI this is intended, but the same helper is invoked from
in-process scripts (`scripts/smoke.py:main`, `scripts/benchmark.py:main`), where
it silently disables the host process's logging with no restoration.

### Uvicorn start failure skips lifespan cleanup

_Covers:_ seq 46, `MAIN` → `HTTPX_CLIENT` (outcome: anomaly)

`build_app` allocates the `httpx.AsyncClient` and the `HeadroomCompressor`
(which eagerly creates its `ThreadPoolExecutor` at construction,
`adapters/headroom.py:HeadroomCompressor.__init__`, line 18) before
`uvicorn.run` is called. The only orderly cleanup is inside the lifespan's
`finally`, which awaits `client.aclose()` and `compressor.close()`
(`bootstrap.py:lifespan`, lines 44–46) — but that body runs only after Uvicorn
has successfully started the app. `main` wraps neither `uvicorn.run` nor
`build_app` in cleanup (`bootstrap.py:main`, lines 71–78), so a bind/start
failure (for example the configured port already in use) aborts the process with
a traceback while the pooled client and compression executor are never closed
through their owning composition root. Process exit reclaims the OS handles, so
the durable consequence is a missing orderly-shutdown step and an absent
operator-facing terminal event rather than a leak across restarts.

## Verification

- `src/switchyard_gateway/bootstrap.py:main` — confirmed argparse options
  `--config`/`--check`, the ordering of `silence_dependency_logs`, the three
  telemetry env writes, the `find_spec("litellm")` guard, the
  `except GatewayError` handler emitting `startup_failed` + `SystemExit(1)`, the
  `--check` `configuration_valid` emit, and
  `uvicorn.run(..., workers=1, access_log=False, log_config=None)`.
- `src/switchyard_gateway/bootstrap.py:build_app` — confirmed construction of
  `httpx.AsyncClient`, `HeadroomCompressor`, `SwitchyardRouter`,
  `HttpxTransport`, `Gateway`, and `create_app`, and the startup/shutdown events
  in `lifespan`.
- `src/switchyard_gateway/bootstrap.py:lifespan` — confirmed the `startup` event
  payload and the `try/finally` that closes the client and compressor.
- `src/switchyard_gateway/adapters/config.py:load_config` — confirmed JSONC
  parsing (`json5.loads(..., allow_duplicate_keys=False)`), `_resolve` env
  handling, exception wrapping into sanitized `GatewayError`s, and
  `Config.settings()`.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents.emit` and
  `silence_dependency_logs` — confirmed one-line JSON serialization to a
  `TextIO` defaulting to `sys.stdout`, and the one-way `logging.disable`
  side effect.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor.__init__` —
  confirmed the eager `ThreadPoolExecutor(max_workers=1)` allocation.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — confirmed the
  lifespan is stored on the FastAPI app, making it the shared CLI/app hinge.
- `pyproject.toml [project.scripts]` and `Dockerfile` `ENTRYPOINT`/`CMD` —
  confirmed the console-script entry point and the container invocation
  (`--config /app/config.jsonc`).
- `scripts/smoke.py:main` and `scripts/container_smoke.py:main` — confirmed the
  offline startup/readiness behavior; `tests/test_config_architecture.py` pins
  config error sanitization and JSON event line shape; no test currently invokes
  `bootstrap.main`, `build_app`, or the `--check` branch.

## Serialization notes

### Group assignment

- `MAIN`, `ARG_PARSER`, `LITELLM_GUARD`, `BUILD_APP`, `GATEWAY`, and `LIFESPAN`
  are placed in group 3. The project legend has no dedicated bootstrap/composition
  group; group 3 is the closest fit for the composition root and the application
  core it constructs, while the concrete adapters it wires are group 4.
- `INGRESS_APP` (`create_app`) is group 2 because the legend assigns FastAPI/ASGI
  boundaries there, even though the factory lives under `adapters/`.
- `CONFIG_FILE`, `ENV`, `STDOUT`, `STDERR`, `UVICORN`, and `THREAD_POOL` are
  group 7 process/OS resources; `SWITCHYARD_SDK`/`HEADROOM_SDK` are group 5
  vendor SDKs; `HTTPX_CLIENT` is group 4 because the legend names HTTPX as an
  adapter.

### Roles

- `OPERATOR` is `initiator` only (initiator-wins): it both starts the lifecycle
  and receives the eventual return/exit, so its termination is derived from the
  incoming return edges rather than declared.
- `STDOUT` and `UVICORN` are `sink`: they are the intended server-side
  terminations (the JSON response surface and the running worker).
- `STDERR` is `dead-end`: malformed invocations terminate there without the
  intended JSON response.

### Seq ordering and kind mapping

- The `--check` branch (seq 14–16) and the server branch (seq 17–38) are
  mutually exclusive but share one total order; the `--check` short-circuit is
  placed first because it terminates earliest. This exclusivity is a
  serialization limitation, not two sequential phases.
- Error links occupy seq 39–43 and anomaly links seq 44–46, after the happy
  path, so outcome filters extract complete subgraphs without interleaving.
- `note` is used for static import/dependency annotations (seq 23, 25) and for
  absent-path findings (seq 45, 46); `event` is used for fire-and-forget writes
  to stdout and for the LiteLLM-present signal (seq 39).

## References

### Specs and decisions

- [README.md §Configuration](README.md) — JSONC rules, env-reference semantics, and "Configuration changes require restart/recreation".
- [README.md §Observability](README.md) — JSON records on stdout and dependency-log suppression.
- [README.md §Verification](README.md) — the offline/container verification commands the startup path feeds.
- [AGENTS.md../../AGENTS.md — contracts for LiteLLM absence, sanitized logs, and bootstrap ownership of adapter lifetimes.
- [CHANGELOG.md../../CHANGELOG.md §1.0.0 — the CLI/JSONC/LiteLLM-free startup as a breaking change.

### Related lifecycles

- [app-lifespan](app-lifespan.md) — shares `build_app` and the `lifespan` context manager this lifecycle starts via `uvicorn.run`.
- [chat-stream](chat-stream.md) — downstream request flow that relies on the adapters and event sink wired here.
- [health-check](health-check.md) — container readiness probe that only succeeds once this startup path has bound the server.
- [model-discovery](model-discovery.md) — downstream read flow over the same wiring.

### Code

- `src/switchyard_gateway/bootstrap.py:main` — the lifecycle entry point.
- `src/switchyard_gateway/bootstrap.py:build_app` — shared composition and lifespan hinge.
- `src/switchyard_gateway/adapters/config.py:load_config` — configuration load and validation.
- `src/switchyard_gateway/adapters/logging.py:JsonEvents` — terminal JSON event sink.
- `src/switchyard_gateway/adapters/logging.py:silence_dependency_logs` — process-global logging suppression.
- `src/switchyard_gateway/adapters/headroom.py:HeadroomCompressor` — compression adapter and worker thread.
- `src/switchyard_gateway/adapters/ingress.py:create_app` — FastAPI app and lifespan ownership.
- `pyproject.toml` — console-script declaration.
- `Dockerfile` — container entrypoint and command.
