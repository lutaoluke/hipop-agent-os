"""hipop.json 加载器：自动展开 ${ENV_VAR} 和 ${ENV_VAR:-default} 占位符。

替代散落在各处的 `json.load(open('hipop.json'))`。

用法:
    from _config import load_config
    cfg = load_config()
    feishu_secret = cfg["feishu"]["app_secret"]   # 已经是 env 真值
"""
from __future__ import annotations

import os
import re
import json
import functools
from typing import Any

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "hipop.json")
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(val: Any) -> Any:
    if isinstance(val, str):
        def repl(m):
            return os.environ.get(m.group(1)) or (m.group(2) or "")
        return _ENV_RE.sub(repl, val)
    if isinstance(val, dict):
        return {k: _expand(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_expand(v) for v in val]
    return val


@functools.lru_cache(maxsize=1)
def load_config(path: str = None) -> dict:
    """读 hipop.json + 展开 env 占位符。结果缓存（重启 server 才会重读 env）。"""
    p = path or CONFIG_PATH
    with open(p) as f:
        raw = json.load(f)
    return _expand(raw)


def reload_config():
    """env 改了想热刷，调这个 + 再 load_config()。"""
    load_config.cache_clear()
