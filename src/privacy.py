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

自由文本防线
------------
`deliveries[].title` 是 commit message / issue 标题，属于外部自由文本，
白名单永远列不全（历史上就有一条 "Merge ... 'upstream/<上游分支>/4.1'"
把真实上游分支名原样写进了公开 state）。因此对展示字段再加一道自检：
命中「配置线索词」的片段换成由配置确定性生成的 token，读盘可还原。
去重主键（deliveries[].id）、水位键、经验台账都不参与这一步，逐字不变。

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
# 不加 \b：scrub_text 是无词边界的精确替换，token 后面可能紧跟字母数字
# （例如把 `componentsXYZ` 里的 `components` 换掉后得到 `r_xxxXYZ`）。
# 带 \b 会让这种 token 读盘时无法还原，进而破坏去重主键。未登记的 token
# 由 unscrub_text 原样返回，放宽边界不会误伤普通文本。
TOKEN_RE = re.compile(r"r_[0-9a-f]{12}")

# 去噪词：配置字段名、纯技术词、公开模板名、占位符。
# 只用于「线索词」与「配置值片段」的去噪，绝不用于过滤真实仓库全名
# （通用词可能正好是真实项目名的一部分，误过滤反而造成泄露）。
NOISE_WORDS = frozenset({
    # 配置字段名及其切分片段
    "owner", "repos", "extra", "repos", "extra_repos", "self", "self_repo",
    "upstream", "candidates", "upstream_candidates", "default", "branches",
    "default_branches", "bots", "comment", "comments", "_comment", "grouping",
    "heartbeat", "minutes", "max", "rules", "aggregation", "error", "errors",
    "categories", "patterns", "level", "title", "true", "false", "null", "none",
    # 纯技术词 / 分支名 / 版本占位
    "main", "master", "dev", "develop", "release", "trunk", "head", "base",
    "latest", "stable", "https", "http", "github", "com", "actions", "action",
    "input", "output", "source", "target", "config", "branch", "remote",
    "tracking", "merge", "update", "sync", "build", "file", "test", "sample",
    # 公开模板自身与常见机器人服务名
    "repo-monitor", "monitor", "github-actions", "dependabot", "renovate",
    "coderabbitai", "coderabbit", "pr-digest", "bot",
    # 模板占位
    "your-org", "your_org", "example", "demo", "repo", "project", "profile-readme",
    "project-b", "repo-a", "repo-b", "repo-c",
})

# 模块级配置：由 config_loader.load_settings() 调用 configure() 注入
_SALT = DEFAULT_SALT
_NAME2TOKEN: dict[str, str] = {}
_TOKEN2NAME: dict[str, str] = {}
# 线索词：长度 >= 6 的配置派生片段，用于展示字段的残留自检。
_SCAN_TOKENS: set[str] = set()
_ENABLED = False


def _token(name: str) -> str:
    digest = hmac.new(_SALT.encode("utf-8"), name.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return "r_" + digest[:12]


def _is_noise(low: str) -> bool:
    return (low in NOISE_WORDS or low.replace(".", "").isdigit()
            or low.startswith(("your-", "your_", "example", "demo-", "sample-")))


def _scan_tokens(cfg: dict) -> set[str]:
    """从配置的「真实标识值」提取长度 >= 6 的线索词。

    只取 >= 6 是有意的：更短的片段（droid/deck/wine/test/extra）几乎都是
    通用英文词，拿去扫自由文本会大面积误伤，反而把正常 commit message
    改写成 token。真实项目名（winlator / droiddeck / emubox ...）都够长。
    """
    out: set[str] = set()

    def feed(s) -> None:
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{4,}", str(s or "")):
            low = w.lower()
            parts = {low} | {p for p in re.split(r"[-._/]", low) if len(p) >= 6}
            for part in parts:
                if len(part) < 6 or _is_noise(part):
                    continue
                out.add(part)

    # 这几个字典的「键」是仓库名（真实标识），必须一起扫；
    # 其余字典的键只是字段名，不扫，避免把字段名切碎当线索。
    repo_keyed = ("upstream", "upstream_candidates", "default_branches")
    for key, val in (cfg or {}).items():
        if key in repo_keyed:
            if isinstance(val, dict):
                for k, v in val.items():
                    if not str(k).startswith("_"):
                        feed(k)
                    feed(v)
            else:
                feed(val)
        else:
            feed(val)
    return out


def find_leaks(text: str) -> list[str]:
    """按词边界扫描文本里残留的配置线索词（大小写不敏感）。

    也供上线门禁使用：用真实配置 configure 后扫公开产物，发现残留即阻断。
    """
    if not _ENABLED or not text or not _SCAN_TOKENS:
        return []
    hits = []
    for w in _SCAN_TOKENS:
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])",
                     text, re.I):
            hits.append(w)
    return sorted(hits, key=len, reverse=True)


def _add_value_fragments(value, names: set[str]) -> None:
    """配置里「值」也要进白名单，不只是键。

    default_branches 的 value 可能是上游分支引用（形如 "vendor-x/4.1"），
    它会原样出现在 commit message 里；历史上只收键，导致真实分支名泄露。
    """
    s = str(value or "").strip()
    if not s or _is_noise(s.lower()):
        return
    names.add(s)
    for seg in re.split(r"[/]", s):
        seg = seg.strip()
        if len(seg) >= 4 and not _is_noise(seg.lower()):
            names.add(seg)


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

    if cfg.get("self_repo"):
        add_repo(cfg["self_repo"])
    for key in ("repos", "extra_repos"):
        for r in cfg.get(key, []) or []:
            add_repo(r)

    for key in ("upstream", "upstream_candidates"):
        for k, v in (cfg.get(key, {}) or {}).items():
            if k.startswith("_"):
                continue
            add_repo(k)
            for item in (v if isinstance(v, list) else [v]):
                add_repo(item)
                _add_value_fragments(item, names)

    # 关键修复：default_branches 的「值」同样可能带真实上游标识（分支引用）。
    for k, v in (cfg.get("default_branches", {}) or {}).items():
        add_repo(k)
        _add_value_fragments(v, names)
    for b in cfg.get("bots", []) or []:
        if b:
            names.add(str(b))

    # 只用「明确的占位符」判断，绝不引用 NOISE_WORDS：
    # 通用技术词可能正好是真实仓库名的一部分，误过滤反而造成明文泄露。
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
    global _SALT, _NAME2TOKEN, _TOKEN2NAME, _SCAN_TOKENS, _ENABLED
    _SALT = salt or os.environ.get("PRIVACY_SALT") or DEFAULT_SALT
    names = collect_identifiers(cfg)
    if enabled is None:
        enabled = any("/" in n for n in names)
    _ENABLED = bool(enabled)
    _SCAN_TOKENS = _scan_tokens(cfg) if _ENABLED else set()
    # 大小写变体 + 线索词入白名单。
    # 配置里常写首字母大写的标识，但外部事件的 id、commit message 里可能
    # 出现小写、全大写或驼峰混合写法。scrub_text 是大小写敏感的精确替换，
    # 只收原始大小写会让这些变体原样留在公开 state 里。
    # 每个变体用独立 token（不是共享一个），unscrub 时逐字还原，去重主键与
    # 水位键因此完全不受影响；线索词一并纳入，结构化主键里的项目简称也能
    # 被可逆投影。全部由配置确定性重算，下一轮 configure() 重建同一张表。
    universe = set(names) | (_SCAN_TOKENS if _ENABLED else set())
    variants: set[str] = set()
    for n in universe:
        if not n:
            continue
        variants.update((n, n.lower(), n.upper(), n.capitalize()))
        # 二分 PascalCase：覆盖配置里没写出来的驼峰混合写法。
        # 只收标准大小写变体会漏，而逐字还原又要求每种形式各有独立 token。
        low = n.lower()
        if 4 <= len(low) <= 14 and low.isalnum():
            for cut in range(1, len(low)):
                variants.add(low[:cut].capitalize() + low[cut:].capitalize())
    _NAME2TOKEN = {}
    _TOKEN2NAME = {}
    # 按长度降序插入：dict 保序，scrub_text 遍历时先替换长串，
    # 避免长标识被短变体截断成半截 token。
    for n in sorted(variants, key=len, reverse=True):
        t = _token(n)
        _NAME2TOKEN[n] = t
        _TOKEN2NAME[t] = n


def enabled() -> bool:
    return _ENABLED


def reset() -> None:
    """测试用：恢复成 no-op。"""
    global _ENABLED, _NAME2TOKEN, _TOKEN2NAME, _SCAN_TOKENS
    _ENABLED, _NAME2TOKEN, _TOKEN2NAME, _SCAN_TOKENS = False, {}, {}, set()


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


def _scrub_residual(s: str) -> str:
    """展示字段残留自检：命中线索词的片段换成可逆 token。

    按长度降序、词边界、大小写不敏感替换。生成的 token 已由 configure()
    登记进反查表，读盘 unscrub_text 能还原，不会破坏任何主键。
    """
    if not _ENABLED or not s:
        return s
    leaks = find_leaks(s)
    if not leaks:
        return s
    out = s
    for w in leaks:
        tok = _token(w)
        out = re.sub(
            r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])",
            tok, out, flags=re.I)
    return out


# 残留自检只作用于「纯展示」字段。绝不触碰：
#   * deliveries[].id / watermarks 的键 / etags 的键 / todos[].id —— 去重与水位主键，
#     其中的真实标识已由白名单精确、可逆地投影；
#   * lessons / sync_decisions —— 经验台账，用户要求「只追加、不覆盖、不精简」；
#   * pending[].lines —— 推送失败时要按原文重试。
FREE_TEXT_FIELDS = ("title", "snippet", "reason", "hint", "advice")
FREE_TEXT_COLLECTIONS = ("todos", "errors")
_REDACTED_RE = re.compile(r"^r_[0-9a-f]{12}$")


def _redact_free_text(s: str) -> str:
    """deliveries[].title 专用：整段压成一个稳定 token。

    该字段是 commit message / issue 标题，纯展示备注，去重只靠 id，
    代码里没有任何地方读取它。白名单永远列不全外部自由文本
    （实测残留过 "upstream/<上游分支>/4.1"、项目简称、内部代号），
    所以整段脱敏是唯一能彻底关闭这个泄露面的做法。

    幂等很关键：读盘后值仍是 token，下一轮写盘必须原样保留，
    否则每轮 state 都变化，会触发无意义的 force push。
    """
    if not s or _REDACTED_RE.match(s):
        return s
    return _token("title:" + s)


def scrub_state(state: dict) -> dict:
    """返回可安全写入公开仓库的副本（不改动内存中的原对象）。

    三道防线：
      1) 全量白名单投影（键与值）—— 可逆，读盘逐字还原；
      2) deliveries[].title 整段幂等脱敏 —— 关闭外部自由文本泄露面；
      3) 其余展示字段残留自检 —— 线索词换成可反查 token。

    只做「替换」，不删条目、不改顺序、不精简任何内容；
    去重主键、水位键、经验台账逐字不变。
    """
    if not _ENABLED:
        return state
    out = _walk(state, scrub_text)
    for item in out.get("deliveries", []) or []:
        if isinstance(item, dict) and isinstance(item.get("title"), str):
            item["title"] = _redact_free_text(item["title"])
        if isinstance(item, dict):
            for field in ("snippet",):
                if isinstance(item.get(field), str):
                    item[field] = _scrub_residual(item[field])
    for coll in FREE_TEXT_COLLECTIONS:
        items = out.get(coll)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in FREE_TEXT_FIELDS:
                v = item.get(field)
                if isinstance(v, str):
                    item[field] = _scrub_residual(v)
    out["_privacy"] = {"projected": True, "salt_hint": _SALT[:8]}
    return out


def unscrub_state(state: dict) -> dict:
    """把读进来的投影状态还原成代码内部使用的真实名。"""
    if not _ENABLED:
        return state
    state.pop("_privacy", None)
    return _walk(state, unscrub_text)
