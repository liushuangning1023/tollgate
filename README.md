# TollGate

**Local AI-call butler: a zero-deploy OpenAI-compatible proxy that meters every call, unifies cost accounting across providers, fails over automatically, and enforces budgets.**

```console
$ tollgate serve
[tollgate] listening on http://127.0.0.1:8787
[tollgate] point your app at  base_url = http://127.0.0.1:8787/v1
[tollgate] tag calls with the X-Tollgate-Tag header for cost attribution

$ tollgate report --day --by tag
tollgate report — last day by tag

  tag          calls  ok  retries  in+out tokens   cost      avg latency
  proj-a         143  141       12        2.1M     $1.8420   840ms
  my-site         89   89        2        480K     $0.3915   310ms
  total                                   2.6M     $2.2335
```

TollGate is for developers and small teams whose AI traffic goes straight
from app to provider — and who then can't answer *"which project spent how
much today?"*, take an outage on the chin, or cap a runaway script.

## What it solves

| Pain | TollGate |
|---|---|
| Cost is invisible; usage logs can't be attributed | Every metered call lands in SQLite tagged by `X-Tollgate-Tag` |
| Providers bill on different token kinds; comparisons mislead | One pricing table (USD/1M in+out) applied to all providers; unknown models are flagged, never silently zero-costed |
| 429 / 5xx / outages take your app down | Exponential backoff honoring `Retry-After`, then failover to the next provider in the route chain |
| Keys hardcoded everywhere; no spend ceiling | Keys live in one config; per-tag and global daily budgets with `alert` or hard `cutoff` |
| Ops-heavy gateways (LiteLLM server, Bifrost, ...) | One file, one process, stdlib HTTP server, `requests` as the only dependency |

## How it works

```
your app ──HTTP──▶ TollGate (localhost:8787)
                    │  route chain for "model":
                    │    1. openai/gpt-4o-mini    ──┐
                    │    2. glm/glm-4-flash       ──┼─▶ first healthy provider wins
                    │    3. deepseek/deepseek-chat──┘
                    │  ✔ meter → SQLite (tag, model, tokens, cost, latency, retries)
                    │  ✔ budgets: pre-call check (cutoff) + post-call alert
                    └──response/SSE──▶ your app
```

- **Transparent**: any `/v1/*` request is forwarded (chat, completions,
  embeddings, models). Change `base_url`, done — no SDK, no code changes.
- **Streaming**: SSE is passed through chunk-by-chunk; usage comes from the
  stream when the upstream provides it, otherwise estimated (chars/4) and
  marked with a `*` in reports.
- **Failover rewrites the model**: `"model": "gpt-4o-mini"` can hop to
  `glm-4-flash` on provider #2 — the chain is yours to define.
- **Metering precedes the reply**: a call is recorded before your app sees
  the response, so billing never races the client.

## Quickstart

```bash
git clone https://github.com/liushuangning1023/tollgate tollgate && cd tollgate
pip install .
tollgate init          # writes ~/.tollgate/config.json
# edit providers: put your real keys in, define routes
tollgate serve
```

Point any OpenAI-compatible client at it:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="whatever")
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "hi"}],
    extra_headers={"X-Tollgate-Tag": "my-project"},   # cost attribution
)
```

Then watch the money:

```bash
tollgate status                      # today in one line
tollgate report --week --by tag      # who spent what
tollgate tail                        # live call log
```

## Configuration

`~/.tollgate/config.json` (JSON, `chmod 600` by `init`; env `TOLLGATE_CONFIG`
relocates it):

```json
{
  "listen": {"host": "127.0.0.1", "port": 8787},
  "db_path": "~/.tollgate/calls.db",
  "providers": {
    "openai":   {"base_url": "https://api.openai.com/v1", "api_key": "sk-..."},
    "glm":      {"base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "..."},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key": "..."},
    "ollama":   {"base_url": "http://localhost:11434/v1", "api_key": "ollama"}
  },
  "provider_order": ["openai", "glm", "deepseek"],
  "routes": {
    "gpt-4o-mini": [
      {"provider": "openai", "model": "gpt-4o-mini"},
      {"provider": "glm", "model": "glm-4-flash"},
      {"provider": "deepseek", "model": "deepseek-chat"}
    ],
    "default": [{"provider": "glm", "model": "glm-4-flash"}]
  },
  "budgets": {
    "global": {"daily_usd": 10.0, "mode": "alert"},
    "tags": {"my-project": {"daily_usd": 1.0, "mode": "cutoff"}}
  },
  "retry": {"max_attempts": 3, "backoff_base": 0.5},
  "request_timeout": 120,
  "pricing_overrides": {"glm-4-flash": {"input": 0.0, "output": 0.0}},
  "local_token": ""
}
```

Rules of the road:

- **Routes**: an exact model match wins; otherwise the `default` route if
  defined; otherwise the model is tried verbatim on every provider in
  `provider_order`. A leading `/v1` in the client path is stripped —
  provider `base_url` already carries its own version prefix.
- **Budgets**: `alert` logs a warning when the daily cap is crossed;
  `cutoff` returns HTTP 429 with an OpenAI-style error *before* the request
  is forwarded (nothing is billed). The strictest verdict (tag vs global)
  wins.
- **Pricing**: the bundled `pricing.json` is a reference convenience table —
  providers change prices constantly, so verify against your bill and
  override per model in the config. Unknown models are metered but flagged
  (cost `*` in reports), never silently treated as free.
- **`local_token`**: if set, clients must present
  `Authorization: Bearer <token>` to TollGate itself (bind to 127.0.0.1 by
  default; this matters if you expose the port).

## Commands

| Command | What it does |
|---|---|
| `tollgate init` | write a starter config (use `--force` to overwrite) |
| `tollgate serve [--host H] [--port P]` | run the proxy |
| `tollgate status` | today's calls / tokens / spend in one line |
| `tollgate report --day\|--week\|--month --by tag\|model_used\|provider\|status` | cost report |
| `tollgate tail [-n N] [--once]` | follow the call log |
| `tollgate budget` | budget table: cap, spent, mode, state |
| `tollgate pricing [--model M]` | pricing table lookup |

## Honest limitations

- Retries on 429/timeouts repeat the request; a timed-out attempt may have
  been partially billed upstream. `max_attempts` bounds this — keep it small
  for expensive models.
- Streaming calls only carry usage if the upstream sends it in the stream
  (OpenAI does with `stream_options.include_usage`); otherwise TollGate
  estimates tokens (chars/4) and marks the cost as estimated (`*`).
- The proxy speaks the OpenAI-compatible dialect. Providers with their own
  wire formats (e.g. native Anthropic Messages API) are not translated.
- No semantic caching, no prompt rewriting, no web dashboard — deliberately.
  See the roadmap.

## Design choices

- **Stdlib HTTP server** + `requests` only. No FastAPI/aiohttp dependency
  tree; the process is small enough to run next to your terminal or as a
  user service.
- **SQLite** storage: query it with any tool, copy the file, ship it to
  DuckDB. No server, no migrations.
- **Meter before reply**: a row exists the moment the upstream answers —
  reports can't undercount due to races.
- **Config normalization**: routes referencing unconfigured providers are
  dropped at load, so failover chains stay honest.
- **61 offline tests** spin up real local HTTP servers (fake OpenAI
  upstream + TollGate) and exercise the actual wire path: forwarding,
  streaming, failover, budgets, auth, tagging. CI runs Python 3.9–3.13.

## Roadmap

- Weekly/monthly budget windows (currently daily)
- Prometheus scrape endpoint
- Response caching for identical prompts (opt-in)
- Multi-key pools per provider with automatic rotation on quota errors

中文文档：[README.zh-CN.md](README.zh-CN.md)。Contributions welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
