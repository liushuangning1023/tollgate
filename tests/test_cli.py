"""CLI: init, status, report, budget, pricing, tail."""

from __future__ import annotations

import json
import time

from tollgate.cli import main
from tollgate.store import CallStore


def seed_db(path, rows):
    store = CallStore(str(path))
    for row in rows:
        store.record(**row)
    store.close()


def cli_env(tmp_path, monkeypatch, config=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(config or {}))
    db_path = tmp_path / "calls.db"
    monkeypatch.setenv("TOLLGATE_CONFIG", str(cfg_path))
    monkeypatch.setenv("TOLLGATE_DB", str(db_path))
    monkeypatch.delenv("TOLLGATE_HOST", raising=False)
    monkeypatch.delenv("TOLLGATE_PORT", raising=False)
    return db_path


class TestInit:
    def test_init_writes_config(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "tollgate.json"
        monkeypatch.setenv("TOLLGATE_CONFIG", str(cfg_path))
        assert main(["init"]) == 0
        payload = json.loads(cfg_path.read_text())
        assert "providers" in payload and "routes" in payload
        assert cfg_path.stat().st_mode & 0o777 == 0o600

    def test_init_refuses_overwrite(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "tollgate.json"
        monkeypatch.setenv("TOLLGATE_CONFIG", str(cfg_path))
        assert main(["init"]) == 0
        assert main(["init"]) == 1
        assert main(["init", "--force"]) == 0


class TestStatusReport:
    ROWS = [
        dict(tag="proj-a", provider="fake", model_used="m1", status=200, ok=True,
             latency_ms=30, prompt_tokens=100, completion_tokens=50,
             cost_usd=0.0004, cost_known=True),
        dict(tag="proj-a", provider="fake", model_used="m1", status=200, ok=True,
             latency_ms=20, prompt_tokens=100, completion_tokens=50,
             cost_usd=0.0004, cost_known=True),
        dict(tag="proj-b", provider="fake", model_used="m2", status=500, ok=False,
             latency_ms=10, error="upstream 500", cost_usd=0.0),
    ]

    def test_status_output(self, tmp_path, monkeypatch, capsys):
        db = cli_env(tmp_path, monkeypatch)
        seed_db(db, self.ROWS)
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "3 calls" in out and "proj-a" in out

    def test_report_by_tag(self, tmp_path, monkeypatch, capsys):
        db = cli_env(tmp_path, monkeypatch)
        seed_db(db, self.ROWS)
        assert main(["report", "--window", "day", "--by", "tag"]) == 0
        out = capsys.readouterr().out
        assert "proj-a" in out and "proj-b" in out and "$0.000800" in out

    def test_report_by_model(self, tmp_path, monkeypatch, capsys):
        db = cli_env(tmp_path, monkeypatch)
        seed_db(db, self.ROWS)
        assert main(["report", "--by", "model_used"]) == 0
        out = capsys.readouterr().out
        assert "m1" in out

    def test_report_empty(self, tmp_path, monkeypatch, capsys):
        cli_env(tmp_path, monkeypatch)
        assert main(["report"]) == 0
        assert "no calls" in capsys.readouterr().out


class TestTail:
    def test_tail_once(self, tmp_path, monkeypatch, capsys):
        db = cli_env(tmp_path, monkeypatch)
        seed_db(db, [dict(tag="x", model_used="m", status=200, ok=True,
                          latency_ms=5, cost_usd=0.001, cost_known=True)])
        assert main(["tail", "-n", "5", "--once"]) == 0
        out = capsys.readouterr().out
        assert "x" in out and "✔" in out


class TestBudgetCommand:
    def test_budget_table(self, tmp_path, monkeypatch, capsys):
        db = cli_env(tmp_path, monkeypatch, config={
            "budgets": {
                "global": {"daily_usd": 5.0, "mode": "alert"},
                "tags": {"proj-a": {"daily_usd": 0.001, "mode": "cutoff"}},
            }
        })
        seed_db(db, [dict(tag="proj-a", cost_usd=0.0005, ok=True, status=200,
                          latency_ms=5, cost_known=True)])
        assert main(["budget"]) == 0
        out = capsys.readouterr().out
        assert "global" in out and "tag:proj-a" in out

    def test_budget_empty(self, tmp_path, monkeypatch, capsys):
        cli_env(tmp_path, monkeypatch)
        assert main(["budget"]) == 0
        out = capsys.readouterr().out
        assert "no budgets configured" in out


class TestPricingCommand:
    def test_pricing_lists_models(self, capsys):
        assert main(["pricing", "--model", "gpt-4o-mini"]) == 0
        out = capsys.readouterr().out
        assert "gpt-4o-mini" in out and "bundled reference" in out

    def test_pricing_no_match(self, capsys):
        assert main(["pricing", "--model", "zzz-nope"]) == 1


class TestMisc:
    def test_no_args_shows_help(self, capsys):
        assert main([]) == 0
        assert "tollgate" in capsys.readouterr().out

    def test_version(self):
        import pytest
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
