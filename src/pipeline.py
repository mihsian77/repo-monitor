#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流水线编排：采集 → 幂等 → 分级 → 建议/错误总结 → 待办 → 沉默/心跳 → 交付 → 水位。

首次运行只建基线不刷屏（沿用 v1 的好设计）：把历史事件静默记为已处理，
避免上线的瞬间把过去几百条事件一次性推到群里。
"""
from __future__ import annotations

import os
from typing import Any

from . import state as st
from .events import GitHubSource
from .notify import FeishuClient, deliver
from .report import (Card, build_cards, build_todos, check_silence,
                     daily_checkin_card, checkin_day, heartbeat_card,
                     summarize_error, todo_summary)
from .rules import classify


# 采集端按「端点」读水位，这里必须按「端点」写，键名一一对应。
# 旧实现只写 {kind}:{repo} 与 global，而 events.py 读的是 commits:{repo} /
# notifications，两边对不上，since 参数永远是空字符串，等于每轮全量拉 100 条。
# 通知中心是跨仓库的单一端点，因此用不带仓库名的全局键。
ENDPOINT_WATERMARKS = {
    "push": "commits",
    "issue": "issues",
    "comment": "comments",
}


def _advance_watermarks(state: dict, events: list) -> None:
    wm = state["watermarks"]
    for e in events:
        ts = e.created_at or ""
        if not ts:
            continue
        keys = [f"{e.kind}:{e.repo}", "global"]
        prefix = ENDPOINT_WATERMARKS.get(e.kind)
        if prefix:
            keys.append(f"{prefix}:{e.repo}")
        if e.kind == "notification":
            keys.append("notifications")
        for key in keys:
            if ts > wm.get(key, ""):
                wm[key] = ts


def _fetch_error_details(source, decisions, rules: dict, enabled: bool) -> dict:
    """对构建失败抓日志并归类。回放/离线时跳过，不产生网络请求。"""
    out = {}
    if not enabled or not isinstance(source, GitHubSource):
        return out
    for d in decisions:
        if d.kind != "run_failure" or d.level == "HEARTBEAT":
            continue
        jobs_url = d.meta.get("jobs_url", "")
        if not jobs_url:
            continue
        resp = source.c.get(jobs_url)
        jobs = (resp.data or {}).get("jobs", []) if isinstance(resp.data, dict) else []
        fj = next((j for j in jobs if j.get("conclusion") == "failure"), None)
        if not fj:
            continue
        log = source.c.get_text(f"/repos/{d.repo}/actions/jobs/{fj.get('id')}/logs")
        if log:
            out[d.event_id] = summarize_error(log, rules)
    return out


def run(source, state: dict, cfg: dict, rules: dict, client: FeishuClient,
        now: str | None = None, bootstrap: bool | None = None,
        save_path: str | None = None, fetch_logs: bool = False) -> dict:
    now = now or st.now_utc()
    if bootstrap is None:
        bootstrap = state["heartbeat"].get("runs", 0) == 0

    events = source.collect(state, cfg)
    # 幂等：同一对象来自事件流 / REST / Webhook 共用语义 ID，这里再收一次
    fresh = [e for e in events if not st.is_notified(state, e.id)]
    decisions = [classify(e, cfg, rules) for e in fresh]

    if bootstrap:
        for d in decisions:
            st.mark_notified(state, d.event_id, "BASELINE", d.title)
        _advance_watermarks(state, events)
        st.heartbeat_ok(state, now)
        if save_path:
            st.save(save_path, state)
        return {"bootstrap": True, "events": len(events), "fresh": len(fresh),
                "cards": [], "note": "首次运行只建基线，未推送历史消息"}

    # DROP 也记账，避免每轮重复决策，但不产生任何卡片
    for d in decisions:
        if d.level == "DROP":
            st.mark_notified(state, d.event_id, "DROP", d.title)
    active = [d for d in decisions if d.level != "DROP"]

    error_details = _fetch_error_details(source, active, rules, fetch_logs)
    cards = build_cards(active, cfg, rules, error_details, now=now)
    new_todos = build_todos(active, state, cfg)

    # 本人回复 = 销账信号：关掉该仓库最老的一条「等人回复」待办
    closed = 0
    for d in active:
        if not d.meta.get("closes_todo"):
            continue
        cands = [t for t in st.open_todos(state)
                 if t.get("repo") == d.repo and t.get("kind") == "human_reply"]
        if cands:
            cands.sort(key=lambda t: t.get("created_at", ""))
            if st.close_todo(state, cands[0]["id"]):
                closed += 1

    cards.extend(check_silence(state, active, cfg, now, rules))
    hb = heartbeat_card(state, cfg, now)
    if hb:
        cards.append(hb)
    checkin = daily_checkin_card(
        state, cfg, now, event_count=len(events),
        upstream_count=sum(1 for d in active if d.kind == "upstream"))
    if checkin:
        cards.append(checkin)

    stats = deliver(state, client, cards, cfg, now)
    # 与所有其他卡片一致：只有真的推送成功才记「今天已报到」。
    # 失败时会留在 pending 里，下一轮补发，绝不提前销账。
    if checkin and st.is_delivered(state, checkin.id):
        state.setdefault("daily", {})["last_report"] = checkin_day(now)
    _advance_watermarks(state, events)
    st.heartbeat_ok(state, now)
    dead = st.prune(state)
    if save_path:
        st.save(save_path, state)

    by_level: dict[str, int] = {}
    for d in decisions:
        by_level[d.level] = by_level.get(d.level, 0) + 1
    return {
        "bootstrap": False,
        "events": len(events), "fresh": len(fresh), "duplicates": len(events) - len(fresh),
        "by_level": by_level,
        "cards": [c.to_dict() for c in cards],
        "card_count": len(cards),
        "new_todos": new_todos,
        "todos_closed": closed,
        "todos": todo_summary(state, now),
        "delivery": stats,
        "dead_letters": dead,
        "heartbeat_failures": state["heartbeat"].get("failures", 0),
        "gh_calls": getattr(getattr(source, "c", None), "stats", lambda: {})(),
    }
