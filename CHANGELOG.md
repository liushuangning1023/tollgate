# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org).

## [0.1.0] — 2026-11-11

### Added
- OpenAI-compatible local proxy: transparent `/v1/*` forwarding with
  optional local bearer auth.
- SSE streaming passthrough with usage extraction (upstream-provided)
  or flagged estimation (chars/4).
- SQLite metering: tag, provider, model, status, latency, tokens,
  cost, retries — recorded before the client sees the response.
- Unified pricing table (USD/1M in+out) with bundled reference data,
  config overrides, and unknown-model flagging.
- Route chains with model rewriting, retry honoring `Retry-After`,
  exponential backoff, and provider failover; unconfigured providers
  are dropped from chains at load.
- Per-tag and global daily budgets with `alert` and pre-call `cutoff`
  (HTTP 429, OpenAI-style error body, nothing billed).
- CLI: `init`, `serve`, `status`, `report` (day/week/month ×
  tag/model/provider/status), `tail` (with `--once`), `budget`,
  `pricing`.
- 61 offline tests exercising the real HTTP wire path against a fake
  OpenAI upstream; CI matrix Python 3.9–3.13.

[0.1.0]: https://github.com/liushuangning1023/tollgate/releases/tag/v0.1.0
