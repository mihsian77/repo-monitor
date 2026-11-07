#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隐私投影层：让 state.json 可以被安全地提交进公开仓库。

问题
----
监控状态里天然带着「我们监控了谁」：watermarks 的 key 是
`push:owner/repo`、known_repos / todos 的 repo 字段、deliveries 的 id 与
title 里都有真实仓库名。而 workflow 每轮会把 state.json commit 回本仓库 ——
如果仓库公开，等于每轮把真实清单重新发布一次。

做法
----
代码内部（采集、去重、水位、卡片）**全程使用真实名**，一行不用改。
只在两个边界做投影：

  * `save()` 写出前：把配置里出现过的真实标识替换成稳定 token（`r_xxxxxxxx`）；
  * `load()` 读入后：按同一张反查表把 token 还原成真实名。

token = HMAC-SHA256(salt, 标识)[:12]，不可逆；反查表由配置里的真实清单重建，
因此只有持有真实配置（Secrets / 本地 private 文件）的一方才能还原。
没有配置时（别人 clone 模板、离线回放）整个模块是 no-op，行为完全不变。

注意
----
`PRIVACY_SALT` 一旦改变，旧 state 里的 token 就无法还原，等价于重置去重账
（历史事件会被当成新事件重新采到）。**上线后不要随意改这个 Secret**；
确实要改，请先手动清空 state.json 并按首轮 bootstrap 重新建基线。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

DEFAULT_SALT = "repo-monitor/v2/privacy-projection/v1"
TOKEN_RE = re.compile(r"\br_[0-9a-f]{12}\b")

# 模块级配置：由 config_loader.load_settings() 调用 configure() 注入
_SALT = DEFAULT_SALT
_NAME2TOKEN: dict[str, str] = {}
_TOKEN2NAME: dict[str, str] = {}
_ENABLED = False


def _token(name: str) -> str:
    digest = hmac.new(_SALT.encode("utf-8"), name.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return "r_" + digest[:12]


def collect_identifiers(cfg: dict) -> list[str]:
    """从配置里收集所有可能出现在 state / 日志中的真实标识。

    按长度降序返回，保证先替换 `owner/repo` 再替换 `owner`，
    避免长标识被短标识截断成半截 token。
    """
    names: set[str] = set()
    owner = str(cfg.get("owner", "") or "")
    if owner:
        names.add(owner)

    def add_repo(r: str) -> None:
        r = str(r or "")
        if not r:
            return
        names.add(r)
        if "/" in r:
            names.add(r.split("/", 1)[1])       # 短名（title 里常不带 owner）
            names.add(r.split("/", 1)[0])       # 上游 owner
        if r.endswith("-bot") or "-bot" in r:
            names.add(r)

    for key in ("self_repo",):
        if cfg.get(key):
            add_repo(cfg[key])
    for key in ("repos", "extra_repos"):
        for r in cfg.get(key, []) or []:
            add_repo(r)

    upstream = cfg.get("upstream", {}) or {}
    for k, v in upstream.items():
        add_repo(k)
        for item in (v if isinstance(v, list) else [v]):
            add_repo(item)

    for k, v in (cfg.get("upstream_candidates", {}) or {}).items():
        if k.startswith("_"):
            continue
        add_repo(k)
        for item in (v if isinstance(v, list) else [v]):
            add_repo(item)

    for k in (cfg.get("default_branches", {}) or {}):
        add_repo(k)
    for b in cfg.get("bots", []) or []:
        if owner and owner in str(b):
            names.add(str(b))

    # 去掉占位符：模板里的示例值不该被当成敏感标识。
    # 用「前缀精确判断」而非子串匹配 —— 否则真实仓库名里恰好含 example/demo
    # 等词会被漏掉，写进公开 state 就等于泄露。
    def is_placeholder(n: str) -> bool:
        low = n.lower()
        return (low.startswith(("your-org", "your_org", "upstream-org",
                                "example", "demo-", "sample-")) or
                low in ("owner", "repo", "repo-a", "repo-b", "repo-c",
                        "project-b", "profile-readme"))
    names = {n for n in names if n and not is_placeholder(n)}
    return sorted(names, key=len, reverse=True)


def configure(cfg: dict, salt: str | None = None,
              enabled: bool | None = None) -> None:
    """注入真实配置并启用投影。

    `enabled` 由 config_loader 依据 is_template() 显式传入，避免靠占位串
    猜模式（连字符/下划线写法不一致曾导致模板状态误启用）。未传时按
    「是否出现真实 owner/repo」推断，保持向后兼容。
    """
    global _SALT, _NAME2TOKEN, _TOKEN2NAME, _ENABLED
    _SALT = salt or os.environ.get("PRIVACY_SALT") or DEFAULT_SALT
    names = collect_identifiers(cfg)
    if enabled is None:
        enabled = any("/" in n for n in names)
    _ENABLED = bool(enabled)
    _NAME2TOKEN = {}
    _TOKEN2NAME = {}
    for n in names:
        t = _token(n)
        _NAME2TOKEN[n] = t
        _TOKEN2NAME[t] = n


def enabled() -> bool:
    return _ENABLED


def reset() -> None:
    """测试用：恢复成 no-op。"""
    global _ENABLED, _NAME2TOKEN, _TOKEN2NAME
    _ENABLED, _NAME2TOKEN, _TOKEN2NAME = False, {}, {}


def scrub_text(s: str) -> str:
    """真实名 → token（写盘 / 打日志前调用）。"""
    if not _ENABLED or not s:
        return s
    out = s
    for name, tok in _NAME2TOKEN.items():
        if name in out:
            out = out.replace(name, tok)
    return out


def unscrub_text(s: str) -> str:
    """token → 真实名（读盘后调用）。"""
    if not _ENABLED or not s or "r_" not in s:
        return s
    return TOKEN_RE.sub(lambda m: _TOKEN2NAME.get(m.group(0), m.group(0)), s)


def _walk(obj, fn):
    """递归处理 dict 的 key 与 value、list 元素、字符串。"""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {fn(k) if isinstance(k, str) else k: _walk(v, fn)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(v, fn) for v in obj)
    return obj


def scrub_state(state: dict) -> dict:
    """返回可安全写入公开仓库的副本（不改动内存中的原对象）。"""
    if not _ENABLED:
        return state
    out = _walk(state, scrub_text)
    out["_privacy"] = {"projected": True, "salt_hint": _SALT[:8]}
    return out


def unscrub_state(state: dict) -> dict:
    """把读进来的投影状态还原成代码内部使用的真实名。"""
    if not _ENABLED:
        return state
    state.pop("_privacy", None)
    return _walk(state, unscrub_text)
