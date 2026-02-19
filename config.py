import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List


def _load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Config:
    data: Dict[str, Any]

    def get(self, path: str, default: Any = None) -> Any:
        cur = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def get_list(self, path: str, default: List[Any]) -> List[Any]:
        val = self.get(path, default)
        return list(val) if isinstance(val, list) else default


def load_config(base_dir: str) -> Config:
    """Load configuration.

    Default: <base_dir>/config.json
    Override: env KIS_CONFIG_PATH (absolute or relative to base_dir)
    """
    _load_env(os.path.join(base_dir, ".env"))

    cfg_path = os.environ.get("KIS_CONFIG_PATH", "").strip()
    if cfg_path:
        if not os.path.isabs(cfg_path):
            cfg_path = os.path.join(base_dir, cfg_path)
    else:
        cfg_path = os.path.join(base_dir, "config.json")

    with open(cfg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Config(data)
