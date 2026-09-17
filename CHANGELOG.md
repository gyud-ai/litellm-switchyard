# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version lives in `pyproject.toml` (`[project] version`).

## [Unreleased]

### Added

- Unit, integration, and smoke test tiers that need no model inference, plus
  Hypothesis property tests for history protection, structural comparison,
  round-robin distribution, routing-tier validation, retry bounds, and cooldown
  parsing.
- A mutation-testing configuration (`[tool.mutmut]`) targeting the routing and
  compression policy in `application.py` and `domain.py`.

## [1.0.0] - 2026-09-17

### Added

- A Python gateway with ports and adapters for routing, compression, transport,
  configuration, HTTP ingress, and JSON observability.
- JSONC configuration for arbitrary named pairs and same-model replicas,
  round-robin selection, cooldowns, and bounded same-model failover.
- Streaming Chat Completions, direct model routes, authentication, protected
  history, structured logs, real adapter contracts, and offline benchmarks.

### Changed

- **Breaking:** replace LiteLLM, Postgres, and the Headroom sidecar with one gateway
  container. Migrate environment/YAML inventories to JSONC. Admin UI, virtual
  keys, spend endpoints, and LiteLLM-specific request/response fields are removed.
- Require Python 3.14 and pin nemo-switchyard 0.2.0 and headroom-ai 0.37.0 without
  LiteLLM or server/proxy/ML extras. Structural compression is enabled by default.
- Publish the gateway image using the project version. Existing database volumes
  are left untouched for rollback.

## [0.3.0] - 2026-09-16

### Added

- Two-pair variant: `profiles/stage/litellm.multipair.yaml` + `.env.multipair`
  (selected via `LITELLM_CONFIG_FILE`), with the second pair following the
  `_2` suffix guideline (`SWITCHYARD_GROUP_2` defaulting to `switchyard_2`).
  The shim stage-routes any configured pair (N-scalable discovery, unset
  pairs skipped); all pairs share one `switchyard.toml` policy.
- Pair-1 group name settable via `SWITCHYARD_GROUP` (default `switchyard`).
- Static sync checks: the multipair variant's pair-1 blocks must equal the
  default file, and every yaml `os.environ/` ref must resolve via
  `compose.yaml` and the matching example env file.

### Changed

- Default inventory is single-pair again (`litellm.yaml` + `.env.example`);
  pair-2 backend vars are optional (empty default) so the default `.env`
  boots without them.

### Fixed

- `compose.yaml` now exports `*_MAX_OUTPUT_TOKENS[_2]` (previously only
  referenced by the yaml/examples, never passed to the proxy).

## [0.1.1] - 2026-09-15

### Added

- Prebuilt images published to GHCR on `main` pushes; `compose.yaml`
  pulls the version-pinned tags by default (`LITELLM_IMAGE` /
  `HEADROOM_IMAGE` override, `--build` compiles locally).

### Fixed

- `restart: unless-stopped` on the `litellm` service (the proxy stayed
  down after a crash while `db` and `headroom` restarted).
- CI skips docs-only changes (`README.md`, `AGENTS.md`, `CHANGELOG.md`,
  `LICENSE`).

## [0.1.0] - 2026-09-15

Initial release: self-hosted LiteLLM proxy with Switchyard stage routing,
plus opt-in Headroom prompt compression.

### Added

- LiteLLM proxy (`litellm==1.97.0`) + Postgres via Docker Compose, with
  master-key auth, spend tracking, and Admin UI (`1bfe2bb`).
- Switchyard stage routing over a single `switchyard` model group: cheap
  efficient tier by default (`efficient_first`, threshold `0.5`), expensive
  capable tier on tool-error/exploration signals; both tiers generic
  OpenAI-compatible endpoints configured via `.env` (`1bfe2bb`).
- `plugins/stage_scoped.py` scoping shim: stage routing applies only to the
  configured cheap/expensive pair; every other model group passes through to
  plain LiteLLM routing (`1bfe2bb`).
- `x-*` client-header forwarding scoped to the `switchyard` group, so
  backends requiring headers like `x-opencode-session` work with plain
  client requests (`1bfe2bb`).
- Headroom compression sidecar (`headroom-ai==0.27.0`) called via a
  `headroom-compression` pre_call guardrail after Switchyard routing;
  tier-agnostic, opt-in via `HEADROOM_DEFAULT_ON` (default `false`)
  (`6f76e67`).
- `LITELLM_IP` bind knob (loopback by default) (`6f76e67`).
- Pytest suite (`uv run --group test pytest tests/`): static config checks
  plus live routing, tool-use loop, OOM-escalation, and guardrail checks
  (`1bfe2bb`, `6f76e67`).
- Apache-2.0 license, quickstart and setup docs.

[Unreleased]: https://github.com/gyud-ai/litellm-switchyard/compare/v1.0.0...HEAD
[0.3.0]: https://github.com/gyud-ai/litellm-switchyard/compare/v0.1.1...v0.3.0
[0.1.1]: https://github.com/gyud-ai/litellm-switchyard/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gyud-ai/litellm-switchyard/releases/tag/v0.1.0

[1.0.0]: https://github.com/gyud-ai/litellm-switchyard/compare/v0.3.0...v1.0.0
