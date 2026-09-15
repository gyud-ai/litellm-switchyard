# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version lives in `pyproject.toml` (`[project] version`).

## [Unreleased]

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

[Unreleased]: https://github.com/gyud-ai/litellm-switchyard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gyud-ai/litellm-switchyard/releases/tag/v0.1.0
