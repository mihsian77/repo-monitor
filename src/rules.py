#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分级规则与建议引擎（纯规则，零 AI 额度）。

示例语料（合成回放数据，约 600 条规范化事件）里的典型分布：
  - 大量 push 来自 bot，且常见 chore 自动刷新；
  - 评论里机器人模板占多数；
  - 外部真人 Issue 数量很少。
所以默认把「真人」当强信号，把「机器人 + chore 模板」压成日报或静默。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .events import Event

LEVELS = ("P0", "P1", "P2", "DROP", "HEARTBEAT")


@dataclass
class Decision:
    event_id: str
    kind: str
    repo: str
    actor: str
    title: str
    level: str
    reason: str = ""
    advice: str = ""
    group: str = ""
    url: str = ""
    created_at: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("event_id", "kind", "repo", "actor", "title", "level",
                 "reason", "advice", "group", "url", "created_at")}


def is_bot(actor: str, cfg: dict, rules: dict) -> bool:
    a = (actor or "").lower()
    if a in {x.lower() for x in rules.get("bot_actor_exact", [])}:
        return True
    if a in {x.lower() for x in cfg.get("bots", [])}:
        return True
    return any(a.endswith(s.lower()) for s in rules.get("bot_actor_suffixes", []))


def _match_any(patterns: list, text: str) -> bool:
    if not text:
        return False
    for p in patterns:
        try:
            if re.search(p, text, re.I):
                return True
        except re.error:
            continue
    return False


def is_noise_title(title: str, rules: dict) -> bool:
    return _match_any(rules.get("noise_title_patterns", []), title or "")


def is_docs_title(title: str, rules: dict) -> bool:
    return _match_any(rules.get("docs_title_patterns", []), title or "")


def files_status(event: Event) -> str:
    """文件清单可信度，三态：

      ok          拿到了可用清单，「没命中」才是真结论
      truncated   清单被 GitHub 截断，只能说「已知命中」，不能说「没命中」
      unavailable 没拿到清单（compare 不可用 / 非 fork / 限流），必须如实说无法判定

    旧版只看 meta["files"]，上游事件没有 files 时会静默得出「没命中敏感路径」，
    这是最危险的假阴性：恰好漏掉的就是改了 C/C++ 的上游更新。
    """
    meta = event.meta or {}
    files = meta.get("files")
    if not isinstance(files, list) or not files:
        return meta.get("files_status") or "unavailable"
    if meta.get("files_truncated"):
        return "truncated"
    return meta.get("files_status") or "ok"


def hits_sensitive(event: Event, rules: dict) -> list[str]:
    """返回命中的敏感文件路径（compare / commit 详情 / Webhook 提供 files）。"""
    files = event.meta.get("files") or []
    pats = rules.get("sensitive_paths", [])
    hit = []
    for f in files:
        if _match_any(pats, f):
            hit.append(f)
    return hit[:8]


def has_version(title: str, rules: dict) -> bool:
    return _match_any(rules.get("version_patterns", []), title or "")


def advise(event: Event, level: str, cfg: dict, rules: dict) -> str:
    """看别人作业时顺手给建议：值不值得看 → 动了我哪儿 → 差多少 → 一句动作。"""
    k, t = event.kind, event.title or ""
    if k == "upstream":
        up = event.meta.get("upstream", "上游")
        hit = hits_sensitive(event, rules)
        fs = files_status(event)
        behind = event.meta.get("behind_by")
        ahead = event.meta.get("ahead_by")
        status = event.meta.get("status", "")
        if is_docs_title(t, rules) and not hit:
            return f"{up} 只改文档/翻译且未命中敏感模块 → 可暂缓，等发布说明出来再说"
        parts = [f"{up} 有新提交"]
        if isinstance(behind, int) and behind > 0:
            parts.append(f"本地落后 {behind} 个提交")
        if isinstance(ahead, int) and ahead > 0:
            parts.append(f"本地另有 {ahead} 个独有提交（合并前先看待同步冲突）")
        if status == "identical":
            parts.append("compare 显示本地与上游已一致，无需同步")
        if hit:
            parts.append(f"命中敏感模块（{', '.join(hit[:3])}）")
        if fs == "truncated":
            parts.append("compare 返回被截断，敏感路径可能还有更多，别当成『没命中』")
        elif fs == "unavailable":
            parts.append("未取到文件清单，无法判定是否动到 C/C++ 或下载引擎")
        if hit:
            parts.append(rules.get("sensitive_hint", ""))
        else:
            parts.append("建议：先看 compare 的落后量与冲突文件，再决定同步")
        return "；".join(x for x in parts if x)
    if k == "push" and level == "P1":
        hit = hits_sensitive(event, rules)
        if has_version(t, rules):
            return "涉及版本号/tag，建议等发布说明确认后再同步；" + rules.get("sensitive_hint", "")
        if hit:
            return f"改动命中 {', '.join(hit[:3])}；" + rules.get("sensitive_hint", "")
        return "人工关键提交，建议先看 diff 再决定是否合入"
    if k == "issue":
        tl = t.lower()
        if any(w in tl for w in ("下载失败", "download", "fail", "error", "崩溃", "crash", "无法")):
            return "疑似报错类反馈：先按「错误总结」把日志压成一句，再复现定位（例如下载 100% 后的连接/握手阶段）"
        if any(w in tl for w in ("compile", "build", "编译", "支持", "request", "能否")):
            return "功能/编译请求类：确认需求归属与工作量后再排期，别让它挂成未回待办"
        return "提问类：建议回复澄清或指引，避免长时间无人应答"
    if k == "release":
        return "同批多个 Release 已聚合；建议核对产物是否随版本一起构建出来"
    if k in ("run_failure",):
        return "构建失败：先看失败 Job 的最后一段日志，再关联是哪次提交/哪些文件带进来的"
    return ""


def classify(event: Event, cfg: dict, rules: dict) -> Decision:
    d = Decision(event_id=event.id, kind=event.kind, repo=event.repo,
                 actor=event.actor, title=(event.title or "")[:160],
                 level="P2", url=event.url, created_at=event.created_at, meta=event.meta)
    bot = is_bot(event.actor, cfg, rules)
    self_repo = cfg.get("self_repo", "")
    k = event.kind

    # ---- 最高优先：不管 actor 是不是机器人 ----
    if k in ("security", "protection"):
        d.level, d.reason = "P0", "安全告警 / 分支保护变更，必须立即处理"
        d.group = "security"
        d.advice = advise(event, d.level, cfg, rules)
        return d

    if k == "run_failure":
        if event.repo == self_repo:
            d.level, d.reason = "HEARTBEAT", "repo-monitor 自身构建失败，走心跳告警通道，不淹没真实告警"
            d.group = "self"
            return d
        d.level, d.reason = "P0", "真实构建失败（已排除监控自身）"
        d.group = "build"
        d.advice = advise(event, d.level, cfg, rules)
        return d

    if k == "notification":
        d.level = "P0" if event.meta.get("type") in ("SecurityAlert", "RepositoryVulnerabilityAlert") else "P2"
        d.reason = f"通知中心：{event.meta.get('reason','')}，已带 all=true 覆盖已读项"
        d.group = "notification"
        return d

    # ---- 上游雷达：与 actor 是不是 bot 无关，只看落后量与文件影响面 ----
    # 旧版把 upstream 放在「外部真人」段末尾，一旦上游作者是 bot 就落到 P2
    # 「上游例行提交」，连敏感路径判定都不做 —— 恰好漏掉改了 C/C++ 的上游更新。
    if k == "upstream":
        hit = hits_sensitive(event, rules)
        fs = files_status(event)
        behind = event.meta.get("behind_by")
        status = event.meta.get("status", "")
        if status == "identical":
            d.level, d.reason = "P2", "上游有新提交，但 compare 显示本地已一致，无需同步"
            d.group = "upstream"
            return d
        d.level = "P1"
        d.group = "upstream_sensitive" if (hit or fs == "truncated") else "upstream"
        if hit:
            d.reason = (f"上游更新命中 {len(hit)} 个敏感路径，"
                        f"本地落后 {behind if isinstance(behind, int) else '?'} 个提交")
        elif fs == "truncated":
            d.reason = "上游更新，compare 文件清单被截断（可能含敏感模块，不能当没命中）"
        elif fs == "unavailable":
            d.reason = "上游有新提交，但文件清单不可用，无法判定敏感路径"
        else:
            d.reason = "上游有新提交"
        d.advice = advise(event, d.level, cfg, rules)
        return d

    # ---- 本人动作：不是「外部真人」，且回复应触发待办销账 ----
    if event.actor == cfg.get("owner", ""):
        if k in ("comment", "review"):
            d.level, d.reason = "P2", "本人回复（触发待办销账，不单独推送）"
            d.group = "self_reply"
            d.meta = {**d.meta, "closes_todo": True}
            return d
        if k == "issue":
            d.level, d.reason = "P2", "本人自建 Issue，并入日报"
            return d
        if k == "pr":
            d.level, d.reason = "P2", "本人日常 PR，并入日报"
            if _match_any(rules.get("sync_pr_patterns", []), event.title or ""):
                d.level, d.reason = "P1", "上游同步 PR（值得看合并结果）"
                d.group = "pr"
            return d

    # ---- 外部真人信号 ----
    if not bot:
        if k in ("issue", "comment", "review"):
            d.level = "P0"
            d.reason = "外部真人 " + ("新建 Issue" if k == "issue" else "留言 / Review")
            d.group = "human"
            d.advice = advise(event, d.level, cfg, rules)
            return d
        if k == "pr":
            d.level, d.reason = "P1", "外部真人 Pull Request"
            d.group = "pr"
            return d
        if k == "push":
            if is_noise_title(event.title, rules):
                d.level, d.reason = "DROP", "chore 自动刷新模板提交，静默"
                return d
            hit = hits_sensitive(event, rules)
            if hit or has_version(event.title, rules):
                d.level, d.reason = "P1", "人工关键提交（命中敏感路径或版本号）"
                d.group = "key_commit"
                d.advice = advise(event, d.level, cfg, rules)
            else:
                d.level, d.reason = "P2", "人工普通提交，并入日报"
            return d
        if k == "release":
            d.level, d.reason = "P1", "新 Release（按批聚合）"
            d.group = "release"
            d.advice = advise(event, d.level, cfg, rules)
            return d

    # ---- 机器人 / 其余 ----
    if k == "comment":
        body = event.body or ""
        if "auto-generated comment" in body.lower() or "coderabbit" in body.lower():
            d.level, d.reason = "DROP", "CodeRabbit 模板审查正文，静默（仅计入日报计数）"
        else:
            d.level, d.reason = "P2", "机器人评论，并入日报"
        return d
    if k == "push":
        d.level, d.reason = "P2", "机器人/自动刷新提交，并入日报"
        return d
    if k == "pr":
        if _match_any(rules.get("sync_pr_patterns", []), event.title or ""):
            d.level, d.reason = "P1", "上游同步 PR（值得看合并结果）"
            d.group = "pr"
        else:
            d.level, d.reason = "P2", "机器人/自动 PR，并入日报"
        return d
    if k == "release":
        d.level, d.reason = "P1", "Release（按批聚合）"
        d.group = "release"
        return d
    if k in ("branch_tag", "social", "run_other", "repo_snapshot"):
        d.level, d.reason = "P2", "例行事件，日报一行"
        return d
    return d
