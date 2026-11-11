"""Command-line interface.

Commands:
  tollgate init            write a starter config (~/.tollgate/config.json)
  tollgate serve           run the local proxy
  tollgate status          quick today summary
  tollgate report          cost/usage report (--day --week --month, --by)
  tollgate tail            follow the call log
  tollgate budget          show budget status
  tollgate pricing         show the pricing table
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__

WINDOW_DAYS = {"day": 1, "week": 7, "month": 30}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tollgate",
        description="Local AI-call butler: metering, cost accounting, failover, budgets.",
    )
    parser.add_argument("--version", action="version", version=f"tollgate {__version__}")
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser("init", help="write a starter config")
    init_parser.add_argument("--force", action="store_true", help="overwrite existing config")
    init_parser.set_defaults(func=cmd_init)

    serve_parser = sub.add_parser("serve", help="run the local proxy")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.set_defaults(func=cmd_serve)

    status_parser = sub.add_parser("status", help="today's quick summary")
    status_parser.set_defaults(func=cmd_status)

    report_parser = sub.add_parser("report", help="cost/usage report")
    report_parser.add_argument("--window", choices=list(WINDOW_DAYS), default="day")
    report_parser.add_argument("--by", choices=["tag", "model_used", "provider", "status"], default="tag")
    report_parser.set_defaults(func=cmd_report)

    tail_parser = sub.add_parser("tail", help="follow the call log")
    tail_parser.add_argument("-n", type=int, default=10, help="show the last N calls first")
    tail_parser.add_argument("--once", action="store_true", help="print current rows and exit (no follow)")
    tail_parser.set_defaults(func=cmd_tail)

    budget_parser = sub.add_parser("budget", help="show budget status")
    budget_parser.set_defaults(func=cmd_budget)

    pricing_parser = sub.add_parser("pricing", help="show the pricing table")
    pricing_parser.add_argument("--model", default=None, help="filter by model substring")
    pricing_parser.set_defaults(func=cmd_pricing)

    return parser


# ------------------------------------------------------------------ helpers
def _load_all(args):
    from .config import load_config

    overrides = {}
    if getattr(args, "host", None):
        overrides["listen_host"] = args.host
    if getattr(args, "port", None):
        overrides["listen_port"] = args.port
    return load_config(overrides=overrides or None)


def _open_store(config):
    from .store import CallStore

    return CallStore(config.db_path)


def _window_start(window: str) -> float:
    return time.time() - WINDOW_DAYS[window] * 86400


def _fmt_usd(value: float) -> str:
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6f}"


def _table(headers: List[str], rows: List[list]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


# ----------------------------------------------------------------- commands
STARTER_CONFIG = {
    "listen": {"host": "127.0.0.1", "port": 8787},
    "db_path": "~/.tollgate/calls.db",
    "providers": {
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-REPLACE-ME",
        },
        "glm": {
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "REPLACE-ME",
        },
    },
    "provider_order": ["openai", "glm"],
    "routes": {
        "gpt-4o-mini": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "glm", "model": "glm-4-flash"},
        ],
        "default": [{"provider": "glm", "model": "glm-4-flash"}],
    },
    "budgets": {
        "global": {"daily_usd": 10.0, "mode": "alert"},
        "tags": {"myproject": {"daily_usd": 1.0, "mode": "cutoff"}},
    },
    "retry": {"max_attempts": 3, "backoff_base": 0.5},
    "request_timeout": 120,
}


def cmd_init(args: argparse.Namespace) -> int:
    from .config import default_config_path

    path = default_config_path()
    if path.exists() and not args.force:
        sys.stderr.write(f"[tollgate] config already exists: {path} (use --force to overwrite)\n")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(STARTER_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print(f"Starter config written to {path}")
    print("Edit providers/routes, set your API keys, then run: tollgate serve")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    config = _load_all(args)
    if not config.providers:
        sys.stderr.write("[tollgate] no providers configured — run `tollgate init` and edit the config.\n")
        return 2

    from .pricing import Pricing
    from .router import Forwarder
    from .server import serve
    from .store import CallStore

    store = _open_store(config)
    pricing = Pricing.bundled(config.pricing_overrides)
    forwarder = Forwarder(config)
    serve(config, store, pricing, forwarder)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_all(args)
    store = _open_store(config)
    try:
        since = time.time() - 86400
        rows = store.since(since)
        calls = len(rows)
        ok = sum(1 for r in rows if r["ok"])
        cost = sum(r["cost_usd"] for r in rows)
        unknown = sum(1 for r in rows if not r["cost_known"])
        retries = sum(r["retries"] for r in rows)
        print(f"last 24h: {calls} calls, {ok} ok, {retries} retries, {_fmt_usd(cost)} spent")
        if calls:
            tags = {}
            for r in rows:
                tags[r["tag"]] = tags.get(r["tag"], 0.0) + r["cost_usd"]
            for tag, spend in sorted(tags.items(), key=lambda kv: -kv[1])[:8]:
                print(f"  {tag:<24} {_fmt_usd(spend)}")
        if unknown:
            print(f"  ({unknown} calls had no pricing entry — add overrides in config)")
    finally:
        store.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config = _load_all(args)
    store = _open_store(config)
    try:
        since = _window_start(args.window)
        rows = store.summarize(since, group_by=args.by)
        label = {"tag": "tag", "model_used": "model", "provider": "provider", "status": "status"}[args.by]
        if not rows:
            print(f"no calls in the last {args.window}")
            return 0
        table_rows = []
        total_cost = 0.0
        total_calls = 0
        for r in rows:
            total_cost += float(r["cost_usd"] or 0)
            total_calls += int(r["calls"])
            table_rows.append([
                r["bucket"] or "-",
                r["calls"],
                f"{int(r['ok_calls'])}",
                int(r["retries"] or 0),
                f"{int(r['prompt_tokens']):,}",
                f"{int(r['completion_tokens']):,}",
                _fmt_usd(float(r["cost_usd"] or 0)),
                f"{int(r['avg_latency_ms'] or 0)}ms",
            ])
        print(f"tollgate report — last {args.window} by {label}\n")
        print(_table(
            [label, "calls", "ok", "retries", "prompt_tok", "completion_tok", "cost", "avg_lat"],
            table_rows,
        ))
        print(f"\ntotal: {total_calls} calls, {_fmt_usd(total_cost)}")
    finally:
        store.close()
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    config = _load_all(args)
    store = _open_store(config)
    try:
        last_id = 0
        rows = store.recent(limit=args.n)
        for r in rows:
            _print_call(r)
            last_id = max(last_id, r["id"])
        if getattr(args, "once", False):
            return 0
        try:
            while True:
                for r in store.after_id(last_id):
                    _print_call(r)
                    last_id = r["id"]
                time.sleep(1.0)
        except KeyboardInterrupt:
            return 0
    finally:
        store.close()


def _print_call(row) -> None:
    stamp = time.strftime("%H:%M:%S", time.localtime(row["ts"]))
    status = row["status"]
    flag = "✔" if row["ok"] else "✗"
    est = "" if row["cost_known"] else "*"
    cost = _fmt_usd(row["cost_usd"]) + est
    print(
        f"{stamp} {flag} {row['tag']:<18} {str(row['provider'] or '-'):<10} "
        f"{str(row['model_used'] or '-'):<22} {status:<4} "
        f"{row['prompt_tokens']:>6}+{row['completion_tokens']:<6} {cost:>10} "
        f"{row['latency_ms']:>6}ms"
        + (f"  retries={row['retries']}" if row["retries"] else "")
    )


def cmd_budget(args: argparse.Namespace) -> int:
    from .budget import summarize_budgets

    config = _load_all(args)
    store = _open_store(config)
    try:
        rows = summarize_budgets(config, store)
        if not rows:
            print("no budgets configured (add 'budgets' to the config)")
            return 0
        print("daily budgets (today):\n")
        table = []
        for scope, _tag, cap, spent, mode in rows:
            pct = (spent / cap * 100) if cap else 0
            state = "OVER" if spent >= cap else ("near" if pct >= 80 else "ok")
            table.append([scope, mode, _fmt_usd(cap), _fmt_usd(spent), f"{pct:.0f}%", state])
        print(_table(["scope", "mode", "cap", "spent", "used", "state"], table))
    finally:
        store.close()
    return 0


def cmd_pricing(args: argparse.Namespace) -> int:
    from .pricing import Pricing

    config = _load_all(args)
    pricing = Pricing.bundled(config.pricing_overrides)
    models = pricing.all_models()
    rows = []
    for model in sorted(models):
        if args.model and args.model.lower() not in model.lower():
            continue
        rates = models[model]
        rows.append([model, f"${rates['input']:.2f}", f"${rates['output']:.2f}"])
    if not rows:
        print(f"no pricing entries matching {args.model!r}")
        return 1
    print(_table(["model", "in /1M", "out /1M"], rows))
    print("\nbundled reference prices — override in config 'pricing_overrides' when they drift")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
