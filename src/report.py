#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错误总结、卡片聚合、待办本、沉默检测、日报。

「光记录作用不大」的对策：
  - 错误不只是抓一行，而是归类 + 可能原因 + 下一步先查哪儿；
  - 待办能销账，每天第一句报「还欠几件、最老几件」；
  - 会报「没发生的事」——自动任务停了、上游久未更新、Issue 久未回复。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import state as st
from .rules import Decision

GROUP_LABEL = {
    "upstream": "上游更新",
    "upstream_sensitive": "上游敏感更新（C/C++ / 构建 / 下载引擎）",
    "key_commit": "人工关键提交",
    "release": "Release 批量发布",
    "pr": "PR / 上游同步",
    "build": "构建失败",
    "human": "真人消息",
    "security": "安全告警",
    "notification": "通知中心",
    "daily": "例行日报",
}

ERROR_NEGATIVE = re.compile(
    r"(?i)(no error|0 errors|without error|errors? (are|were|is) (not|never)|avoid(ing)? error|prevent(ing)? error|error[- ]free)"
)
# Actions 的通用包装行，不是真正的失败原因，不能当错误片段
WRAPPER_LINE = re.compile(
    r"(?i)(##\[error\]process completed|##\[section\]|post job cleanup|exit code \d+\.?$)"
)
TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*")


@dataclass
class Card:
    id: str
    level: str
    title: str
    lines: list = field(default_factory=list)
    event_ids: list = field(default_factory=list)
    group: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "level": self.level, "title": self.title,
                "lines": self.lines, "event_ids": self.event_ids, "group": self.group}


def summarize_error(text: str, rules: dict) -> dict:
    """把构建日志压成一句人话：类别 + 可能原因/下一步 + 关键片段。

    v1 只取第一行带 error 的字，常抓到「0 errors」；这里从尾部失败段取，
    排除否定行、去重堆栈，并按类别匹配。
    """
    empty = {"category": "", "name": "未归类", "hint": "看失败 Job 的最后一段日志，定位首个真实报错",
             "snippet": ""}
    if not text:
        return empty
    lines = [TIMESTAMP_PREFIX.sub("", x).strip() for x in text.splitlines()]
    lines = [x for x in lines if x and not ERROR_NEGATIVE.search(x)]
    tail = lines[-120:] if len(lines) > 120 else lines

    # 优先取明确的失败/错误行，去掉重复的堆栈帧
    candidates = []
    for ln in tail:
        if WRAPPER_LINE.search(ln):
            continue
        if re.search(r"(?i)(\berror\b|fatal|failed|failure|exception|undefined reference|cannot find|no such file|permission denied|timed out)", ln):
            c = ln[:300]
            if c not in candidates:
                candidates.append(c)

    # 按「命中关键词的长度」逐行打分：越具体越优先。
    # 既避免通用的 error: 抢在 undefined reference 之前，也让 snippet 落在最具体的报错行。
    best_score, best_cat, best_line = 0, None, ""
    for ln in tail:
        if WRAPPER_LINE.search(ln):
            continue
        for cat in rules.get("error_categories", []):
            for p in cat.get("patterns", []):
                try:
                    m = re.search(p, ln, re.I)
                    if m and len(m.group(0)) > best_score:
                        best_score, best_cat, best_line = len(m.group(0)), cat, ln[:300]
                except re.error:
                    continue
    snippet = best_line or (candidates[-1] if candidates else (tail[-1][:300] if tail else ""))
    if best_cat:
        return {"category": best_cat["key"], "name": best_cat["name"],
                "hint": best_cat["hint"], "snippet": snippet}
    return {**empty, "snippet": snippet}


def build_cards(decisions: list[Decision], cfg: dict, rules: dict,
                error_details: dict | None = None, now: str = "") -> list[Card]:
    """P0 真人逐条、构建失败按仓库合并、心跳合并；P1 分组；P2 一条日报；DROP 不出卡片。

    合并逻辑沿用 v1 的好设计：同一轮同一仓库多次失败不刷屏。
    所有「周期性卡片」ID 都带时间片，避免被去重永久吞掉。
    """
    error_details = error_details or {}
    stamp = (now or "")[:13]
    cards: list[Card] = []
    p0_build: dict[str, list[Decision]] = defaultdict(list)
    p1: dict[str, list[Decision]] = defaultdict(list)
    p2: list[Decision] = []
    maxp1 = rules.get("aggregation", {}).get("p1_max_items_per_card", 8)
    maxp2 = rules.get("aggregation", {}).get("p2_digest_max_items", 12)

    for d in decisions:
        # HEARTBEAT 决策不再产生卡片：classify() 已把管家自身 run_failure
        # 降为 DROP，自身健康由工作流层 P1 与 12 小时心跳卡独立覆盖。
        # 万一旧 state 里还残留该级别，这里也只静默跳过，不刷 P0 红卡。
        if d.level == "HEARTBEAT":
            continue
        if d.level == "P0" and d.kind == "run_failure":
            p0_build[d.repo].append(d)
            continue
        if d.level == "P0":
            lines = [f"仓库：{d.repo}", f"类型：{d.kind} / {d.actor}",
                     f"原因：{d.reason}", f"时间：{d.created_at}"]
            if d.title:
                lines.append(f"标题：{d.title}")
            if d.advice:
                lines.append(f"建议：{d.advice}")
            if d.url:
                lines.append(f"链接：{d.url}")
            err = error_details.get(d.event_id)
            if err:
                lines.append(f"错误归类：{err['name']}")
                lines.append(f"关键片段：{err['snippet']}")
                lines.append(f"下一步：{err['hint']}")
            cards.append(Card(f"card:{d.event_id}", "P0", f"【P0】{d.repo} · {d.kind}",
                              lines, [d.event_id], d.group))
        elif d.level == "P1":
            p1[d.group or d.kind].append(d)
        elif d.level == "P2":
            p2.append(d)

    # 构建失败：同仓库合并成一条
    for repo, ds in sorted(p0_build.items()):
        ds.sort(key=lambda x: x.created_at or "")
        lines = [f"仓库：{repo}", f"本轮检测到 {len(ds)} 次构建失败，合并为一条："]
        for d in ds[:6]:
            lines.append(f"  · {(d.created_at or '')[:16]} "
                         f"{d.meta.get('workflow', d.title)}@{d.meta.get('branch', '?')}")
        if len(ds) > 6:
            lines.append(f"  …另 {len(ds) - 6} 次未列出")
        err = next((error_details[d.event_id] for d in ds if d.event_id in error_details), None)
        if err:
            lines.append(f"错误归类：{err['name']}")
            lines.append(f"下一步：{err['hint']}")
        elif ds[0].advice:
            lines.append(f"建议：{ds[0].advice}")
        if ds[0].url:
            lines.append(f"链接：{ds[0].url}")
        cards.append(Card(f"card:build:{repo}:{stamp}", "P0",
                          f"【P0】构建失败 · {repo.rsplit('/', 1)[-1]}（{len(ds)} 次）",
                          lines, [d.event_id for d in ds], "build"))

    # 管家自身故障不再单独发卡：classify() 已降为 DROP，自身健康由
    # 工作流层「本轮巡检未跑完」P1 与 heartbeat_card() 的 12 小时超时卡
    # （按天去重、中性标题）独立覆盖。这里不保留任何 P0 红卡出口。

    for group, ds in sorted(p1.items()):
        ds.sort(key=lambda x: x.created_at or "")
        lines = [f"本组共 {len(ds)} 条："]
        for d in ds[:maxp1]:
            bit = f"· {d.repo.rsplit('/', 1)[-1]}：{d.title or d.kind}"
            if d.advice:
                bit += f" → {d.advice}"
            lines.append(bit[:220])
        if len(ds) > maxp1:
            lines.append(f"…另 {len(ds) - maxp1} 条未列出")
        if group == "release":
            lines.insert(0, "同一批发布的多个版本已合并，避免逐条刷屏")
        cards.append(Card(f"card:p1:{group}:{stamp}", "P1",
                          f"【P1】{GROUP_LABEL.get(group, group)}（{len(ds)} 条）", lines,
                          [d.event_id for d in ds], group))

    if p2:
        counts: dict[str, int] = defaultdict(int)
        for d in p2:
            counts[d.kind] += 1
        lines = [f"本轮共 {len(p2)} 条例行事件（不逐条推）：",
                 "  " + "，".join(f"{k} {v}" for k, v in sorted(counts.items()))]
        for d in p2[:maxp2]:
            lines.append(f"  · {d.repo.rsplit('/', 1)[-1]} {d.kind}：{d.title[:60]}")
        cards.append(Card(f"card:p2:daily:{stamp}", "P2", "【P2】例行日报", lines,
                          [d.event_id for d in p2], "daily"))
    return cards


# 机器自动产生的事件类型：它们只在卡片/日报里通知，不进人工待办本。
# 这些事件永远等不到「本人回复」这个销账信号，记成待办就是永久挂账：
# 线上实测 81 件未销账待办里 52 件 build_recovery、29 件 upstream_sync，
# 没有一件是真人消息——待办本彻底沦为只增不减的噪声账。
MACHINE_TODO_KINDS = ("build_recovery", "upstream_sync")


def build_todos(decisions: list[Decision], state: dict, cfg: dict) -> int:
    """待办本只收「需要人动手、且能被销账」的事：真人消息待回、真人 PR 待审。

    构建失败、上游更新、Release 这类机器自动信号，本身已在 P0/P1 卡片里
    通知过（含错误归类与建议），且没有对应的销账路径，重复记一笔待办
    既不增加信息，又会无限堆积。它们改由日报里的「反复发作 N 次」汇总呈现。
    """
    n = 0
    for d in decisions:
        if d.level not in ("P0", "P1"):
            continue
        kind = ""
        if d.kind in ("issue", "comment", "review") and d.level == "P0":
            kind = "human_reply"
        elif d.kind == "pr":
            kind = "pr_review"
        if not kind:
            continue
        tid = f"todo:{d.event_id}"
        before = len(state["todos"])
        st.add_todo(state, tid, kind, d.repo, d.title or d.reason,
                    d.created_at, d.url)
        n += len(state["todos"]) - before
    return n


def close_machine_todos(state: dict) -> int:
    """一次性结清历史遗留的机器类待办，只追加关闭标记，绝不删条目。

    幂等：已关闭的不再动。旧条目保留全部原始字段，额外加 closed /
    closed_at / close_reason，保证「哪天、为什么被结清」可追溯。
    """
    n = 0
    for t in state.get("todos", []):
        if t.get("closed") or t.get("kind") not in MACHINE_TODO_KINDS:
            continue
        if st.close_todo(state, t["id"],
                         reason="auto: 机器自动事件不再计入人工待办本"):
            n += 1
    return n


def todo_summary(state: dict, now: str) -> dict:
    ref = st.parse_ts(now) or datetime.now(timezone.utc)
    opens = st.open_todos(state)
    ages = []
    for t in opens:
        ts = st.parse_ts(t.get("created_at", ""))
        ages.append((ref - ts).days if ts else 0)
    oldest = max(ages) if ages else 0
    return {"open": len(opens), "oldest_days": oldest,
            "escalated": sum(1 for a in ages if a >= 7),
            "items": [f"{t['repo'].rsplit('/', 1)[-1]} · {t['kind']} · {t['title'][:40]}"
                      for t in opens[:5]]}


def check_silence(state: dict, decisions: list[Decision], cfg: dict,
                  now: str, rules: dict) -> list[Card]:
    """报「没发生的事」。几乎不额外调接口，拿本地水位一比就知道。"""
    th = cfg.get("thresholds", {})
    ref = st.parse_ts(now) or datetime.now(timezone.utc)
    day = (now or "")[:10]  # 沉默告警按天可重复，不被去重永久吞掉
    cards: list[Card] = []
    silence = state.setdefault("silence", {})

    # 更新最近一次观察到自动任务的时间
    for d in decisions:
        if d.kind == "push" and "bot" in d.actor.lower():
            silence["bot_push"] = max(silence.get("bot_push", ""), d.created_at or "")
        if d.kind == "upstream":
            silence[f"upstream:{d.repo}"] = max(
                silence.get(f"upstream:{d.repo}", ""), d.created_at or "")

    last_bot = st.parse_ts(silence.get("bot_push", ""))
    if last_bot and (ref - last_bot).total_seconds() > th.get("bot_job_silence_hours", 26) * 3600:
        eid = f"silence:bot:{day}"
        cards.append(Card(f"card:{eid}", "P0", "【P0】自动任务疑似停摆",
                          ["最近一次机器人数据刷新/同步提交距今已超过阈值。",
                           f"最后观察到：{silence.get('bot_push','')}",
                           "安静不代表正常，建议检查对应的定时任务。"],
                          [eid], "silence"))

    stale_up = []
    for local in cfg.get("upstream", {}):
        last = st.parse_ts(silence.get(f"upstream:{local}", ""))
        if last and (ref - last).days > th.get("upstream_silence_days", 21):
            stale_up.append(((ref - last).days, local))
    if stale_up:
        eid = f"silence:upstream:{day}"
        lines = [f"{len(stale_up)} 个上游久无动静，留意是否停止维护："]
        for days, local in sorted(stale_up, reverse=True)[:3]:
            lines.append(f"  · {local.rsplit('/', 1)[-1]}（{days} 天）")
        cards.append(Card(f"card:{eid}", "P2", "【P2】上游久无动静",
                          lines, [eid], "silence"))

    waiting, overdue = [], []
    for t in st.open_todos(state):
        ts = st.parse_ts(t.get("created_at", ""))
        if not ts:
            continue
        days = (ref - ts).days
        if t.get("kind") == "human_reply" and days >= th.get("issue_unanswered_days", 3):
            waiting.append((days, t))
        elif days >= th.get("todo_escalate_days", 7):
            overdue.append((days, t))

    # 同类沉默告警合并成一张，最多列最老的 3 条，避免一次性刷屏
    if waiting:
        eid = f"silence:waiting:{day}"
        lines = [f"还有 {len(waiting)} 件真人消息未销账，最老的 {max(x[0] for x in waiting)} 天："]
        for days, t in sorted(waiting, key=lambda x: x[0], reverse=True)[:3]:
            lines.append(f"  · {t['repo'].rsplit('/', 1)[-1]} · {t['title'][:50]}（{days} 天）")
        lines.append("建议今天处理，或在待办本里明确关闭。")
        cards.append(Card(f"card:{eid}", "P0", "【P0】还有人等你回复 / 待办未销账",
                          lines, [eid], "silence"))
    if overdue:
        eid = f"silence:overdue:{day}"
        lines = [f"{len(overdue)} 件待办已超期："]
        for days, t in sorted(overdue, key=lambda x: x[0], reverse=True)[:3]:
            lines.append(f"  · {t['repo'].rsplit('/', 1)[-1]} · {t['kind']}（{days} 天）")
        cards.append(Card(f"card:{eid}", "P1", "【P1】待办已超期", lines, [eid], "silence"))
    return cards


def heartbeat_card(state: dict, cfg: dict, now: str) -> Card | None:
    """只在「管家彻底挂了」时告警，且一天最多一条。

    GitHub Actions 的 schedule 是尽力而为：实测公开仓库成功巡检间隔可达
    3.5–7 小时。原实现用 90 分钟阈值 + 按小时生成 ID，导致每一轮成功运行
    都必然先报一次「没上班」，并且同一天内反复刷屏 —— 属于纯噪声。
    现在按天去重，阈值由配置放宽到 12 小时；真正需要人介入的判据交给
    「每日在岗」卡（每天必达，没收到才说明管家挂了）。
    """
    th = cfg.get("thresholds", {})
    max_minutes = th.get("heartbeat_max_minutes", 720)
    if not st.heartbeat_stale(state, max_minutes, now):
        return None
    day = (now or "")[:10]  # 按天去重：同一天最多一条
    eid = f"heartbeat:stale:{day}"
    if st.is_notified(state, eid):
        return None
    last = state["heartbeat"].get("last_success", "")
    return Card(f"card:{eid}", "P0",
                f"【P0】管家超过 {max_minutes // 60} 小时未成功巡检",
                [f"上次成功运行：{last or '无记录'}",
                 f"当前：{now}",
                 "连续这么久没有一次成功巡检，才需要去 Actions 查运行记录；",
                 "正常的 GitHub 调度延迟（数小时内）不会再触发这条告警。"],
                [eid], "heartbeat")


def checkin_day(now: str) -> str:
    """按北京时间取「天」，与配置里的 quiet_hours.tz 一致。"""
    dt = st.parse_ts(now)
    if not dt:
        return (now or "")[:10]
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")


def daily_checkin_card(state: dict, cfg: dict, now: str,
                       event_count: int = 0, upstream_count: int = 0) -> Card | None:
    """每日必达在岗确认：就算一整天没有任何事件，也要发一条，让「安静」有含义。

    飞书安静不等于没有事件，也可能等于管家挂了。这条卡片把两者分开：
    收到 = 今天巡检确实跑过；没收 = 去查 Actions。
    按北京时间一天一条；只有推送成功后 pipeline 才会写 daily.last_report。
    """
    if not cfg.get("daily_checkin", {}).get("enabled", True):
        return None
    day = checkin_day(now)
    daily = state.setdefault("daily", {})
    if daily.get("last_report") == day:
        return None
    eid = f"daily:checkin:{day}"
    if st.is_notified(state, eid):
        return None
    todos = todo_summary(state, now)
    lines = [
        f"日期：{day}（北京时间）｜本轮巡检正常完成",
        f"采集端点事件 {event_count} 条，上游映射 {len(cfg.get('upstream', {}))} 个"
        f"（本轮上游事件 {upstream_count} 条）",
        f"待办本：还欠 {todos['open']} 件，最老 {todos['oldest_days']} 天，"
        f"超期 {todos['escalated']} 件",
        f"上次成功运行：{state['heartbeat'].get('last_success') or '首次'}",
        "这条每天必达：收到即表示管家在岗；没收到请到 Actions 查运行记录，",
        "不要默认『没消息就是没事』。",
    ]
    return Card(f"card:{eid}", "P2", "【每日在岗】repo-monitor 正常", lines,
                [eid], "daily_checkin")
