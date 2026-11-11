# Contributing to TollGate

TollGate stays deliberately small — one process, one config, one SQLite
file. Before proposing large new subsystems, please open an issue first.

## Ground rules

- **Dependency budget:** runtime deps stay at `requests` only. The HTTP
  server is the stdlib's; keep it that way unless there is a compelling
  reason.
- **No vendor SDKs** in the forwarding path — TollGate speaks the
  OpenAI-compatible wire format directly.
- **Bugs in money math are release blockers.** Cost/budget changes need
  regression tests with exact numeric assertions.

## Development setup

```bash
git clone https://github.com/liushuangning1023/tollgate tollgate
cd tollgate
pip install -e ".[dev]"
pytest
```

The suite is fully offline: tests spin up a fake OpenAI upstream and a
TollGate instance on localhost and exercise the real HTTP path.

## Where things live

| Area | File |
|---|---|
| HTTP proxy handler, SSE passthrough | `tollgate/server.py` |
| Routing, retry, failover | `tollgate/router.py` |
| Pricing + bundled table | `tollgate/pricing.py`, `tollgate/pricing.json` |
| SQLite metering | `tollgate/store.py` |
| Budget logic | `tollgate/budget.py` |
| Config loading/normalization | `tollgate/config.py` |
| CLI (reports, tail, budget) | `tollgate/cli.py` |

## Commit style

Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).
Tests must pass before pushing.

## Adding a provider preset

Presets are just documentation/config examples — no code changes needed
unless the provider deviates from the OpenAI-compatible dialect. If it
does, open an issue with a captured request/response pair first.

## Reporting a vulnerability

Open a private security advisory rather than a public issue. Include the
exact request sequence that reproduces it.
