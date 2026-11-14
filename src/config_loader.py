#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置加载：仓库内只放模板，真实清单从外部注入。

这个仓库同时有两个用途：
  1. 我们自己跑监控 —— 真实仓库清单绝不能出现在公开仓库里；
  2. 别人当模板参考 —— clone 下来就能读懂、能跑自检和回放。

因此 `config/settings.json` 里只有占位值，真实清单走两条注入通道
（都不入库）：

  * GitHub Actions：`MONITOR_CONFIG` Secret，值为一段 JSON；
  * 本机运行：`config/settings.local.json`（已在 .gitignore 里）。

只有下列「身份类」字段允许被覆盖；阈值、分组、免打扰等行为参数仍以
仓库内模板为准，避免 Secret 里夹带东西改变程序行为。
"""
from __future__ import annotations

import json
import os

# 允许被外部注入的身份类字段
IDENTITY_KEYS = (
    "owner", "repos", "extra_repos", "upstream", "upstream_candidates",
    "default_branches", "bots", "self_repo",
)

PLACEHOLDER_OWNER = "your-org"


def is_template(cfg: dict) -> bool:
    """是否仍是未注入真实清单的模板状态。"""
    owner = str(cfg.get("owner", ""))
    repos = cfg.get("repos", []) or []
    return (owner == PLACEHOLDER_OWNER or not owner or
            any(PLACEHOLDER_OWNER in str(r) for r in repos))


def _payload_from_env() -> dict | None:
    raw = os.environ.get("MONITOR_CONFIG", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit("MONITOR_CONFIG 不是合法 JSON：%s" % e)
    if not isinstance(data, dict):
        raise SystemExit("MONITOR_CONFIG 必须是 JSON 对象")
    return {k: data[k] for k in IDENTITY_KEYS if k in data}


def _payload_from_file(root: str) -> dict | None:
    p = os.path.join(root, "config", "settings.local.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return {k: data[k] for k in IDENTITY_KEYS if k in data}


def load_settings(root: str) -> dict:
    """读模板 → 叠加外部注入 → 配置隐私投影，返回最终 cfg。"""
    with open(os.path.join(root, "config", "settings.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    override = _payload_from_env() or _payload_from_file(root)
    if override:
        cfg.update(override)

    # 启用隐私投影（必须在采集/读状态之前）。
    # 模板状态 = 明确关闭：别人 clone 后行为与旧版完全一致，不会被哈希搅乱。
    from . import privacy
    privacy.configure(cfg, enabled=not is_template(cfg))
    return cfg
