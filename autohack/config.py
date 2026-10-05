"""题目配置的读写（YAML）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

CONFIG_NAME = "config.yaml"

DEFAULT_CHECKS = {"wa": True, "tle": True, "mle": True, "re": True}


@dataclass
class Config:
    problem: str = ""
    kind: str = "standard"          # standard / custom
    gen: str | None = None          # 自定义生成器（相对题目目录）
    tl_ms: int = 2000
    ml_mb: int = 256
    rounds: int = 500
    max_hits: int = 1
    seed: int | None = None
    alphabet: str = "abcdefghijklmnopqrstuvwxyz"
    checks: dict = field(default_factory=lambda: dict(DEFAULT_CHECKS))
    vars: dict = field(default_factory=dict)
    note: str = ""

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        data = asdict(self)
        data["checks"] = {k: bool(v) for k, v in self.checks.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict, problem: str = "") -> Config:
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        cfg = cls(**clean)
        cfg.problem = cfg.problem or problem
        checks = dict(DEFAULT_CHECKS)
        checks.update({k: bool(v) for k, v in (data or {}).get("checks", {}).items()})
        cfg.checks = checks
        return cfg

    def range_of(self, name: str) -> dict:
        return dict(self.vars.get(name, {}) or {})


def config_path(problem_dir: Path) -> Path:
    return Path(problem_dir) / CONFIG_NAME


def load_config(problem_dir: Path) -> Config | None:
    path = config_path(problem_dir)
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise TypeError(f"{path} 内容不是合法的 YAML 映射")
    return Config.from_dict(data, problem=Path(problem_dir).name)


def save_config(problem_dir: Path, cfg: Config) -> Path:
    path = config_path(problem_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg.to_dict(), fh, allow_unicode=True,
                       sort_keys=False, default_flow_style=False)
    return path
