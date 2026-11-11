"""Config resolution precedence and env overrides."""

from __future__ import annotations

import json

from tollgate.config import Config, load_config


def write_config(tmp_path, payload, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("TOLLGATE_CONFIG", str(path))
    return path


class TestConfig:
    def test_defaults(self):
        cfg = Config()
        assert cfg.listen_port == 8787
        assert cfg.max_attempts == 3

    def test_file_overrides_defaults(self, tmp_path, monkeypatch):
        write_config(
            tmp_path,
            {"providers": {"x": {"base_url": "http://x/v1", "api_key": "k"}},
             "listen": {"port": 9999}},
            monkeypatch,
        )
        monkeypatch.delenv("TOLLGATE_HOST", raising=False)
        monkeypatch.delenv("TOLLGATE_PORT", raising=False)
        monkeypatch.delenv("TOLLGATE_DB", raising=False)
        cfg = load_config()
        assert cfg.listen_port == 9999
        assert cfg.providers["x"].api_key == "k"

    def test_env_beats_file(self, tmp_path, monkeypatch):
        write_config(tmp_path, {"listen": {"port": 9999}}, monkeypatch)
        monkeypatch.setenv("TOLLGATE_PORT", "7777")
        monkeypatch.setenv("TOLLGATE_DB", "/tmp/elsewhere.db")
        cfg = load_config()
        assert cfg.listen_port == 7777
        assert cfg.db_path == "/tmp/elsewhere.db"

    def test_invalid_port_env_ignored(self, tmp_path, monkeypatch):
        write_config(tmp_path, {"listen": {"port": 1234}}, monkeypatch)
        monkeypatch.setenv("TOLLGATE_PORT", "not-a-number")
        cfg = load_config()
        assert cfg.listen_port == 1234

    def test_unknown_provider_in_route_dropped(self):
        cfg = Config(
            providers={"p1": {"base_url": "http://u/v1", "api_key": "k"}},
            routes={"m": [{"provider": "p1", "model": "m"},
                          {"provider": "ghost", "model": "m"}]},
            provider_order=["p1"],
        )
        chain = cfg.route_for("m")
        assert [h.provider for h in chain] == ["p1"]

    def test_missing_file_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOLLGATE_CONFIG", str(tmp_path / "missing.json"))
        monkeypatch.delenv("TOLLGATE_PORT", raising=False)
        cfg = load_config()
        assert cfg.listen_port == 8787
