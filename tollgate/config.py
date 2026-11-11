"""Configuration: defaults < ~/.tollgate/config.json < environment.

The config is plain JSON (zero dependencies, works everywhere)::

    {
      "listen": {"host": "127.0.0.1", "port": 8787},
      "db_path": "~/.tollgate/calls.db",
      "providers": {
        "openai":  {"base_url": "https://api.openai.com/v1", "api_key": "sk-..."},
        "glm":     {"base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "..."},
        "deepseek":{"base_url": "https://api.deepseek.com/v1", "api_key": "..."}
      },
      "provider_order": ["openai", "glm", "deepseek"],
      "routes": {
        "gpt-4o-mini": [
          {"provider": "openai", "model": "gpt-4o-mini"},
          {"provider": "glm", "model": "glm-4-flash"},
          {"provider": "deepseek", "model": "deepseek-chat"}
        ]
      },
      "budgets": {
        "global":     {"daily_usd": 10.0, "mode": "alert"},
        "tags":       {"myproject": {"daily_usd": 1.0, "mode": "cutoff"}}
      },
      "retry": {"max_attempts": 3, "backoff_base": 0.5, "timeout": 120},
      "pricing_overrides": {"gpt-4o-mini": {"input": 0.15, "output": 0.60}}
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_PATH = Path("~/.tollgate/config.json").expanduser()

DEFAULTS: Dict[str, Any] = {
    "listen_host": "127.0.0.1",
    "listen_port": 8787,
    "db_path": "~/.tollgate/calls.db",
    "providers": {},
    "provider_order": [],
    "routes": {},
    "budgets": {},
    "max_attempts": 3,
    "backoff_base": 0.5,
    "request_timeout": 120,
    "pricing_overrides": {},
    "local_token": "",
}


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str

    def to_dict(self) -> Dict[str, str]:
        return {"base_url": self.base_url, "api_key": self.api_key}


@dataclass
class Hop:
    provider: str
    model: str


@dataclass
class Config:
    listen_host: str = DEFAULTS["listen_host"]
    listen_port: int = DEFAULTS["listen_port"]
    db_path: str = DEFAULTS["db_path"]
    providers: Dict[str, Provider] = field(default_factory=dict)
    provider_order: List[str] = field(default_factory=list)
    routes: Dict[str, List[Hop]] = field(default_factory=dict)
    budgets: Dict[str, Any] = field(default_factory=dict)
    max_attempts: int = DEFAULTS["max_attempts"]
    backoff_base: float = DEFAULTS["backoff_base"]
    request_timeout: int = DEFAULTS["request_timeout"]
    pricing_overrides: Dict[str, Any] = field(default_factory=dict)
    local_token: str = ""
    config_path: Optional[str] = None

    def __post_init__(self) -> None:
        """Normalize dict-shaped input into Provider/Hop and drop hops
        whose provider is not configured (keeps failover chains honest)."""
        if self.providers and any(not isinstance(v, Provider) for v in self.providers.values()):
            normalized: Dict[str, Provider] = {}
            for name, p in self.providers.items():
                if isinstance(p, Provider):
                    normalized[name] = p
                elif isinstance(p, dict):
                    normalized[name] = Provider(
                        name, str(p.get("base_url", "")), str(p.get("api_key", ""))
                    )
            self.providers = normalized

        if self.routes and any(
            any(not isinstance(h, Hop) for h in hops) for hops in self.routes.values()
        ):
            normalized_routes: Dict[str, List[Hop]] = {}
            for model, hops in self.routes.items():
                chain: List[Hop] = []
                for h in hops:
                    if isinstance(h, Hop):
                        prov_name, hop_model = h.provider, h.model
                    elif isinstance(h, dict):
                        prov_name, hop_model = h.get("provider", ""), h.get("model", "")
                    else:
                        continue
                    if prov_name in self.providers:
                        chain.append(Hop(str(prov_name), str(hop_model)))
                if chain:
                    normalized_routes[model] = chain
            self.routes = normalized_routes

        if not self.provider_order:
            self.provider_order = list(self.providers)
        else:
            self.provider_order = [n for n in self.provider_order if n in self.providers]

    # ------------------------------------------------------------------ api
    def provider(self, name: str) -> Optional[Provider]:
        return self.providers.get(name)

    def route_for(self, model: str) -> List[Hop]:
        """Resolve the hop chain for a requested model.

        Exact route wins; otherwise the ``default`` route if defined;
        otherwise unknown models are tried verbatim on every provider
        in ``provider_order`` (pass-through attempt).
        """
        hops = self.routes.get(model)
        if hops:
            return list(hops)
        default_hops = self.routes.get("default")
        if default_hops:
            return list(default_hops)
        chain = []
        for name in self.provider_order:
            provider = self.providers.get(name)
            if provider is not None:
                chain.append(Hop(provider=name, model=model))
        return chain

    def tag_budget(self, tag: str) -> Optional[Dict[str, Any]]:
        return (self.budgets.get("tags") or {}).get(tag)

    @property
    def global_budget(self) -> Optional[Dict[str, Any]]:
        return self.budgets.get("global")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "listen": {"host": self.listen_host, "port": self.listen_port},
            "db_path": self.db_path,
            "providers": {k: v.to_dict() for k, v in self.providers.items()},
            "provider_order": self.provider_order,
            "routes": {k: [{"provider": h.provider, "model": h.model} for h in v]
                       for k, v in self.routes.items()},
            "budgets": self.budgets,
            "retry": {
                "max_attempts": self.max_attempts,
                "backoff_base": self.backoff_base,
                "timeout": self.request_timeout,
            },
            "pricing_overrides": self.pricing_overrides,
            "local_token": self.local_token,
        }


def load_config(config_path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Config:
    path = Path(config_path).expanduser() if config_path else _default_path()
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    env = _env_config()

    cfg = Config(
        listen_host=data.get("listen", {}).get("host", DEFAULTS["listen_host"]),
        listen_port=int(data.get("listen", {}).get("port", DEFAULTS["listen_port"])),
        db_path=data.get("db_path", DEFAULTS["db_path"]),
        providers={
            name: Provider(name=name, base_url=str(p.get("base_url", "")), api_key=str(p.get("api_key", "")))
            for name, p in (data.get("providers") or {}).items()
            if isinstance(p, dict)
        },
        provider_order=list(data.get("provider_order") or []),
        routes={
            model: [Hop(provider=str(h.get("provider", "")), model=str(h.get("model", "")))
                    for h in hops if isinstance(h, dict)]
            for model, hops in (data.get("routes") or {}).items()
            if isinstance(hops, list)
        },
        budgets=dict(data.get("budgets") or {}),
        max_attempts=int(data.get("retry", {}).get("max_attempts", DEFAULTS["max_attempts"])),
        backoff_base=float(data.get("retry", {}).get("backoff_base", DEFAULTS["backoff_base"])),
        request_timeout=int(data.get("retry", {}).get("timeout", DEFAULTS["request_timeout"])),
        pricing_overrides=dict(data.get("pricing_overrides") or {}),
        local_token=str(data.get("local_token", "")),
        config_path=str(path) if path.exists() else None,
    )
    _apply_env(cfg, env)
    if overrides:
        _apply_overrides(cfg, overrides)
    return cfg


def save_config(cfg: Config, path: Optional[str] = None) -> Path:
    target = Path(path).expanduser() if path else _default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = cfg.to_dict()
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def default_config_path() -> Path:
    """Resolve the config path at call time (honours TOLLGATE_CONFIG)."""
    return _default_path()


def _default_path() -> Path:
    env = os.environ.get("TOLLGATE_CONFIG")
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_PATH


def _env_config() -> Dict[str, Any]:
    env: Dict[str, Any] = {}
    if os.environ.get("TOLLGATE_HOST"):
        env["listen_host"] = os.environ["TOLLGATE_HOST"]
    if os.environ.get("TOLLGATE_PORT"):
        try:
            env["listen_port"] = int(os.environ["TOLLGATE_PORT"])
        except ValueError:
            pass
    if os.environ.get("TOLLGATE_DB"):
        env["db_path"] = os.environ["TOLLGATE_DB"]
    return env


def _apply_env(cfg: Config, env: Dict[str, Any]) -> None:
    for key, value in env.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)


def _apply_overrides(cfg: Config, overrides: Dict[str, Any]) -> None:
    for key, value in overrides.items():
        if value is None:
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)
