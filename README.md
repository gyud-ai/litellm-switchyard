# litellm-switchyard

A self-hosted LiteLLM proxy that routes every request through NVIDIA **Switchyard stage routing**: a cheap *efficient* tier serves routine turns, an expensive *capable* tier takes over on exploration, errors, and hard reasoning. Both tiers are generic OpenAI-compatible endpoints configured entirely via `.env`.

```
client (OpenAI SDK / coding agent)
  │  POST /v1/chat/completions  {"model": "switchyard", ...}
  ▼
litellm:4000  (LiteLLM proxy + switchyard-litellm plugin, authed, Postgres-backed)
  │  1. stage router: tool-error / exploration signals → capable,
  │                    settled turns → efficient (pristine client messages)
  │  2. headroom-compression pre_call guardrail → POST http://headroom:8787/v1/compress
  │     (in-flight only; client history stays pristine for next-turn routing)
  ├──► capable:   ${EXPENSIVE_API_BASE} / ${EXPENSIVE_MODEL_ID}  (cloud, $$)
  └──► efficient: ${CHEAP_API_BASE}     / ${CHEAP_MODEL_ID}      (local, cheap)
```

Response bodies keep `"model": "switchyard"`; the `x-litellm-model-name` response header shows which tier actually served the turn. `x-litellm-applied-guardrails: headroom-compression` means the guardrail was *scheduled* (opt-in requests only) — it does not prove compression executed; the spend-log row's `guardrail_information` is the effect-level record (see Compression).

Upstream reference: `NVIDIA-NeMo/Switchyard/examples/litellm` (pins `litellm==1.97.0`). The standalone `switchyard-server` binary is demo-only, so routing runs as a LiteLLM router plugin *inside* the proxy. One local addition: `plugins/stage_scoped.py` scopes stage routing to the configured pairs so other model groups (including GUI-added ones) pass through to plain LiteLLM routing untouched.

## Layout

```
Dockerfile                  # litellm:1.97.0 + Switchyard wheel (pinned ref, build-time smoke test)
Dockerfile.headroom         # headroom-ai[proxy]==0.27.0 sidecar (pinned, per LiteLLM headroom docs)
compose.yaml                # litellm + postgres + headroom sidecar
.env.example -> .env        # all secrets + backend URLs (never committed)
plugins/stage_scoped.py     # scopes stage routing to the configured pairs only
profiles/stage/litellm.yaml    # default inventory: one pair, 2 deployments (capable FIRST) + headroom guardrail
profiles/stage/litellm.multipair.yaml # two-pair variant (with .env.multipair, via LITELLM_CONFIG_FILE)
profiles/stage/switchyard.toml # stage policy (file-only, no GUI equivalent)
tests/                      # pytest suite: static config checks + live proxy checks
pyproject.toml              # test tooling (uv)
```

## Quickstart

Prerequisites: Docker with Compose v2, and [`uv`](https://docs.astral.sh/uv/) if you want to run the tests.

```bash
cp .env.example .env
# edit .env (see table below), then:
docker compose up -d --wait   # pulls prebuilt GHCR images (~2.4 GB first run)
curl -fsS http://127.0.0.1:4000/health/liveliness   # "I'm alive!"

curl -i http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"switchyard","messages":[{"role":"user","content":"Reply with the word hello."}],"max_tokens":256}'
```

Success looks like: HTTP 200, body `"model": "switchyard"`, and a `x-litellm-model-name` header naming the tier that served the turn (cheap tier on a clean first turn). Admin UI: `http://127.0.0.1:4000/ui`.

```bash
docker compose down        # stop, keep postgres data
docker compose down -v     # stop and wipe postgres data
```

## Setup reference

### `.env` variables

| Variable | Purpose |
|---|---|
| `LITELLM_MASTER_KEY` | Proxy admin key (auth for API + UI) |
| `POSTGRES_PASSWORD` | DB password. **URL-safe chars only** (`A-Za-z0-9_-`); it is embedded into `DATABASE_URL`, so `@ : / ? # %` break Prisma with `P1013` |
| `LITELLM_IP` | Host bind (default `127.0.0.1` loopback-only; `0.0.0.0` or a LAN IP serves the network — no TLS, master key is the only auth, prefer a tunnel/VPN) |
| `LITELLM_PORT` | Host port (default `4000`) |
| `CHEAP_API_BASE` / `CHEAP_API_KEY` / `CHEAP_MODEL_ID` | Efficient tier endpoint. Bare model ID; compose prepends the `openai/` provider prefix |
| `EXPENSIVE_API_BASE` / `EXPENSIVE_API_KEY` / `EXPENSIVE_MODEL_ID` | Capable tier endpoint, same format |
| `SWITCHYARD_GROUP` | Pair-1 routed group name (default `switchyard`) |
| `LITELLM_CONFIG_FILE` | Inventory selector (default `litellm.yaml`); set `litellm.multipair.yaml` with `.env.multipair` for two pairs |
| `CHEAP_REASONING_EFFORT` / `EXPENSIVE_REASONING_EFFORT` | Reasoning depth per tier: `low`, `high`, `max` (defaults `low` / `max`; backend may honor a subset) |
| `CHEAP_MAX_INPUT_TOKENS` / `EXPENSIVE_MAX_INPUT_TOKENS` | Served context window per tier (defaults `131072` / `1048576`; read from each backend's `/v1/models` where available) |
| `CHEAP_MAX_OUTPUT_TOKENS` / `EXPENSIVE_MAX_OUTPUT_TOKENS` | Declared per-turn output cap per tier (defaults `32768` / `262144`; verify against your backend — declarative, not enforced server-side) |
| `CHEAP_MODEL_TIMEOUT` / `EXPENSIVE_MODEL_TIMEOUT` | Per-tier request timeout in seconds (default `300`) |
| `CHEAP_MODEL_RETRIES` / `EXPENSIVE_MODEL_RETRIES` | Per-tier retry count (default `2`) |
| `SWITCHYARD_LITELLM_PROFILE` | Profile dir under `profiles/` (default `stage`) |
| `SWITCHYARD_REF` | Switchyard commit/tag for the Docker builder (default: pinned, tested ref) |
| `HEADROOM_PORT` | Host loopback port for sidecar health/direct probes (default `8788`, never public) |
| `HEADROOM_LOG_LEVEL` | Sidecar log level (default `warning`, use `info` when debugging) |
| `HEADROOM_DEFAULT_ON` | `true` = compress every request on both tiers; `false` (default) = opt-in per request/key. Exactly `true`/`false`, never empty (empty fails proxy startup). Takes effect on `docker compose up -d` (config re-read on recreate) |

Changing models, keys, or endpoints later = edit `.env`, `docker compose up -d`. No YAML/TOML edits needed. (If you change `POSTGRES_PASSWORD` after first boot, use `docker compose down -v` first — Postgres bakes the initial password into its data volume.)

### Tiers and routing policy

`litellm.yaml` declares exactly **two** deployments under the routed group, and **order is the capable/efficient contract** (capable first, efficient second — LiteLLM 1.97 preserves declaration order and Switchyard relies on it). The group lives under `os.environ/SWITCHYARD_GROUP` (default `switchyard`, settable in `.env`); `switchyard.toml` holds only policy (shared by all pairs), no model IDs. Two single-deployment groups named by the backend IDs themselves (`os.environ/CHEAP_MODEL`, `os.environ/EXPENSIVE_MODEL`) address each tier directly and bypass routing entirely (the shim only fires on an exact pair) — use them for debugging, per-tier evals, or clients that want a fixed tier. Never give a direct group both pair IDs.

### Multiple pairs

For two cheap/expensive pairs, copy `.env.multipair` to `.env` instead (it sets `LITELLM_CONFIG_FILE=litellm.multipair.yaml`, which selects `profiles/stage/litellm.multipair.yaml`). The second pair follows the `_2` suffix guideline (`CHEAP_*_2` / `EXPENSIVE_*_2`, group `SWITCHYARD_GROUP_2` defaulting to `switchyard_2`) with the same order contract and its own two direct groups; both pairs share the one `switchyard.toml` policy. Sync rule: yaml `model:` values must equal the compose-composed strings (the `openai/` prefix lives in `compose.yaml`), group names must equal `SWITCHYARD_GROUP[_N]` — the static suite checks both files. N=3+ needs matching `_3` blocks in compose, the multipair yaml, and env.

```toml
algorithm = "stage"
picker = "efficient_first"   # cost-first default; escalate only on signal
confidence_threshold = 0.5
recent_window = 3
```

When the capable tier takes over (from the stage-router signal table):

| Tool-result signal | Examples | Effect at 0.5 |
|---|---|---|
| Critical (hard override) | `out of memory`, `connection refused` | instant escalation |
| Hard errors | tracebacks, `ModuleNotFoundError`, timeouts, missing files | escalate once corroborated |
| Spinning / exploring | repeated failures with no reads/writes, read-only investigation | pushes toward capable |
| Tests passed + recent writes | green pytest output | pushes back to efficient |

A turn with no tool history takes the picker's default (efficient). Tune via `confidence_threshold` (lower = cheaper, higher = safer) in `profiles/stage/switchyard.toml`, then `docker compose restart litellm`. Each deployment also carries endpoint-verified `model_info` (served context windows, reasoning and tool support — no cost fields, since backends bill their own credits; text-only `supports_vision: false` defaults, flip in YAML per backend). The reasoning depth of each tier is env-owned (`CHEAP_REASONING_EFFORT` default `low`, `EXPENSIVE_REASONING_EFFORT` default `max`) and sent via `extra_body` (LiteLLM rejects the bare param for `openai/`-prefixed custom models); the current cheap backend accepts but ignores it. Override per request with your own `extra_body`, or disable thinking the same way.

### Multiple endpoints for the same model

Give every endpoint serving the same model the **identical** model string and differentiate only via `api_base` — LiteLLM then load-balances/fails over across them, and the router still sees one tier. Different strings (another prefix, a `-host-b` suffix) look like different tiers and break the pair.

### Compression (Headroom guardrail)

`litellm.yaml` defines one `headroom-compression` guardrail (`guardrail: headroom`, `mode: pre_call`, `api_base: os.environ/HEADROOM_API_BASE` → `http://headroom:8787`), per https://docs.litellm.ai/docs/proxy/headroom. It runs **after** routing on pristine messages, compresses in-flight via `POST /v1/compress`, and LiteLLM forwards the result to the backend directly — backends stay wired as before, no second proxy hop per tier. The guardrail is **tier-agnostic**: it applies to cheap and expensive turns alike whenever the request opts in. `default_on: false` is kept deliberately (cheap local turns gain prefill speed but pay a sidecar roundtrip — see Latency below).

Opt-in and bypass: per-request `{"guardrails": ["headroom-compression"]}`, per-key attach (`/key/generate` with the same field), or per-call `x-headroom-bypass: true`, which only ever switches compression *off* for that call. Set `HEADROOM_DEFAULT_ON=true` + `docker compose up -d` to compress everything; the YAML value stays `os.environ/HEADROOM_DEFAULT_ON` (LiteLLM interpolates whole values only; the string is coerced to bool — verified live in-container — and `=true` was proven end-to-end: an unmarked compressible call recorded `success` + 472 tokens saved with no opt-in).

What actually gets sent (live-turn protection, LiteLLM 1.97 behavior): system rows, the last user row, the last assistant row, and their whole tool exchanges are **held back** and go to the provider intact — only *older* history compresses. Consequences: single-turn chats and one-exchange tool turns compress to ~0 by design (nothing compressible was sent, not a malfunction); savings appear once older tool/JSON/log history accumulates. Short/code/grep-shaped blocks pass through, `cache_control`-marked blocks are always skipped (provider KV-cache matching preserved), and `/v1/compress` ignores `system`/`tools` schemas, so no CCR retrieve-tool or tool-search injection comes through this path. Measured on the 200-row repetitive-JSON shape: 5690 → 2492 tokens (56% saved) via SmartCrusher.

How to tell it ran (all verified live, 14/14 green): the spend-log row (`GET /spend/logs`) carries `applied_guardrails: ["headroom-compression"]` plus `guardrail_information` with `guardrail_status: success` and `tokens_before/after/saved` — that object is the proof. The response header and the Admin UI Logs → Guardrails panel confirm *scheduling*. The sidecar's own `/stats` counters track only its proxy paths, **not** `/v1/compress` guardrail calls — `0` there does not mean idle. A bypassed call records no `guardrail_information` at all.

Failure mode (observed, not just documented): with the sidecar stopped, guarded requests still return HTTP 200 uncompressed with no `guardrail_information` — fail-open in practice despite the `fail_closed` default on paper. Monitor `guardrail_information`, not HTTP status: a missing object on an opted-in call means compression silently didn't happen. `docker compose logs litellm` stays quiet at default log level either way.

Sidecar requirements (already in `compose.yaml`): `HEADROOM_COMPRESS_ALLOW_REMOTE=1` (else `/v1/compress` 404s from another container — looks like a wrong URL, not a block), `HEADROOM_COMPRESS_USER_MESSAGES=1` (else agent `user`-role traffic barely compresses), local-only telemetry on + beacon off (`HEADROOM_BEACON=off`, `DO_NOT_TRACK=1`).

Latency notes: expect prefill/TTFT wins on long contexts on both tiers (fewer prompt tokens), governed by two costs — the extra LiteLLM→sidecar hop per call, and the Kompress ML tail on prose (one observed guardrail call took ~2.8s; structural SmartCrusher calls take milliseconds). Short prompts can net out slower. Output/decode tokens are untouched by this path. If the tail matters more than maximum savings, set `HEADROOM_DISABLE_KOMPRESS=1` (structural only); per-tier profiles would need a second sidecar.

### Client headers

LiteLLM strips unknown request headers by default. `litellm.yaml` enables `forward_client_headers_to_llm_api` for the routed groups, so any `x-*` header (e.g. `x-session-id`, required by some backends) passes straight to the provider — no `extra_headers` body workaround needed. Forwarding is global across groups (per-group lists can't hold env-valued group names — LiteLLM never interpolates inside string lists). `Authorization` is never forwarded by this mechanism.

### Auth, database, Admin UI

`general_settings` sets the master key, `DATABASE_URL`, and `store_model_in_db: true`, so the UI at `/ui` manages virtual keys, teams, spend tracking, and *unrelated* models without restarts. Keep the routed groups themselves YAML-owned: the shim logs a warning and skips stage routing (rather than failing) if a group is edited into anything but its exact pair — check `docker compose logs litellm` if routing seems off after UI edits.

### Sizing

Measured idle without Headroom: proxy ~720 MiB, postgres ~70 MiB; images ~2.4 GB total. The Headroom sidecar adds ~0.6–1 GB (ONNX embedder; x86 needs AVX2), so run **4 vCPU / 8 GB RAM with a 30–40 GB disk**, no GPU needed. 4/4 boots but goes tight under parallel live tests. Images come prebuilt from GHCR (see `.github/workflows/publish.yml`); pass `--build` only when Dockerfiles or build args (`SWITCHYARD_REF`) change.

### Tests

```bash
uv run --group test pytest tests/ -m "not live"   # static config checks, no server
uv run --group test pytest tests/ -m live          # live proxy checks (sources .env,
                                                   # spends real backend calls)
```

Static checks pin the deployment order/count, plugin registration, TOML policy, Headroom guardrail shape (pre_call, opt-in), and compose/Dockerfile wiring. Live checks assert liveliness, the model group, a chat round trip inside the pair, the cheap-default contract, tool-use loops (echo + multi-step calculator), and OOM-escalation to the expensive tier with header forwarding. Guardrail live checks (`tests/test_headroom_guardrail.py`) assert the sidecar is healthy, opt-in preserves every routing verdict (cheap stays cheap, OOM still escalates, loops stay in-pair), bypass records no compression in spend logs, and a two-exchange 200-row JSON transcript records `tokens_saved > 0` in `guardrail_information` — all read from spend-log rows, since headers only prove scheduling. Run both after any redeploy. (Port `4000` may be taken by another checkout; override with `LITELLM_PORT=<free>`, which the tests honor.)

### Troubleshooting

- `P1013: invalid port number in database URL` → `POSTGRES_PASSWORD` has URL-breaking characters; switch to `A-Za-z0-9_-` and `down -v` + `up`.
- `model=openai/os.environ/...` in logs / cost-map warnings → LiteLLM only interpolates values that *start with* `os.environ/`; the `openai/` prefix lives in `compose.yaml`, keep it that way.
- Backend 400 about a missing session/routing header → send it as a plain HTTP header (forwarded to the provider for all groups) or check the backend's own requirements.
- `Switchyard scope: candidate pool ...` warning → a multi-deployment group stopped matching its pair (usually a UI edit to a routed group); restore two deployments, capable first. Single-deployment groups never warn.
- Headroom `/v1/compress` 404 from LiteLLM → sidecar missing `HEADROOM_COMPRESS_ALLOW_REMOTE=1` (remote callers get 404, not 403, by design).
- Guardrail `guardrail_response.tokens_saved` stays 0 → either the sidecar is missing `HEADROOM_COMPRESS_USER_MESSAGES=1`, or the payload was all live-turn (newest exchange is held back by design — needs older history to compress). Single-turn hello compressing to 0 is expected.
- No `x-litellm-applied-guardrails` header → one of three things: request didn't opt in (`HEADROOM_DEFAULT_ON=false` needs per-request `guardrails` / per-key attach), there was nothing compressible (fully-protected short turns return early *without* the header even when scheduled), or bypass was sent. The header proves scheduling only; a present header with bypass sent or sidecar down means *no* compression happened — check `guardrail_information` in the spend log.
- Litellm boot-loops after touching Headroom env → `HEADROOM_DEFAULT_ON` empty or non-boolean; must be exactly `true`/`false` (compose defaults unset/empty to `false`).
- Opted-in call with no `guardrail_information` in its spend row → compression silently didn't run (sidecar down is observed fail-open: HTTP 200 uncompressed). Check `docker compose ps headroom` and restart it.
- `docker compose config` validates interpolation without starting anything. For the compression side, check the spend-log row (`/spend/logs` → `guardrail_information`: success + `tokens_saved`) or Admin UI Logs → Guardrails panel — the sidecar's own `/stats` counters only track its proxy paths, not `/v1/compress` guardrail calls, so `0` there does not mean the guardrail is idle.
- `pull access denied` for a `ghcr.io/gyud-ai/*` image → the GHCR package may still be private (flip it to public under the package settings) or the publish workflow hasn't run yet; until then, `docker compose up -d --build` compiles locally.

### Updating the Switchyard pin

`SWITCHYARD_REF` (`.env`, default in `compose.yaml`) pins the upstream commit the Docker builder clones. Two known drift points when moving it: upstream's Dockerfile smoke test for `algorithms.random(...)` is stale (this repo instead loads the bundled `switchyard.toml` through the real loader at build time), and the base image is Wolfi (BusyBox `adduser`, not Debian `useradd`). After bumping, rebuild and run the full suite.

## License & attribution

Apache-2.0 — see [LICENSE](LICENSE). Release history lives in [CHANGELOG.md](CHANGELOG.md).

This repo builds on three upstream projects:

- [NVIDIA Switchyard](https://github.com/NVIDIA-NeMo/Switchyard) (Apache-2.0) — routing algorithms and the `switchyard-litellm` plugin this image embeds; `Dockerfile` is adapted from its `examples/litellm` deployment.
- [LiteLLM](https://github.com/BerriAI/litellm) (MIT) — the proxy itself (`ghcr.io/berriai/litellm:v1.97.0` base image).
- [Headroom](https://github.com/headroomlabs-ai/headroom) (Apache-2.0) — context compression called via LiteLLM's `headroom` pre_call guardrail (`Dockerfile.headroom` pins `headroom-ai[proxy]==0.27.0` per [LiteLLM docs](https://docs.litellm.ai/docs/proxy/headroom)).

Model traffic flows to backends you configure; their terms apply.
