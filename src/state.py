#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state v2：每端点独立水位 + 有序事件日志 + pending 重试 + 待办/台账。

关于「只追加、不覆盖、不精简」：
  - deliveries / lessons / sync_decisions 均只追加；
  - 裁剪只按时间窗口丢弃过期条目，绝不从 set 转 list 后随机截断；
  - v1 的 notified_ids 会被完整迁移成带时间的历史条目，不丢任何 ID。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = 2
KEEP_DAYS = 90


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def default_state() -> dict:
    return {
        "version": VERSION,
        "created_at": now_utc(),
        "heartbeat": {"last_success": "", "last_attempt": "", "runs": 0,
                      "failures": 0, "cron_expected": 0, "cron_actual": 0},
        "daily": {"last_report": ""},  # 每日在岗确认已送达的日期（只追加式标记）
        "watermarks": {},          # 每个端点独立水位，互不拖累
        "deliveries": [],          # 有序、带时间戳、只追加
        "pending": [],             # 推送失败待重试
        "todos": [],               # 待办本
        "lessons": [],             # 日期 | 错在哪 | 正确做法
        "sync_decisions": [],      # 上游同步决策账
        "known_repos": {},
        "repo_baseline": {},
        "upstream": {},
        "auto_upstream": {},
        "etags": {},
        "silence": {},             # 上次观察到各类自动任务的时间
    }


def load(path: str) -> dict:
    if not os.path.exists(path):
        return default_state()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # 状态损坏不能静默吞掉：重建骨架但把损坏文件留作 .bad
        try:
            os.replace(path, path + ".bad")
        except Exception:
            pass
        return default_state()
    # 公开仓库里存的是投影态（真实标识已被替换为不可逆 token），
    # 这里按真实配置还原成代码内部使用的名字，去重/水位逻辑完全不受影响。
    try:
        from . import privacy
        data = privacy.unscrub_state(data)
    except Exception:
        pass  # 投影层不可用时按原样读取（模板 / 离线回放不受影响）
    if data.get("version") != VERSION:
        data = migrate_v1(data)
    for k, v in default_state().items():
        data.setdefault(k, v)
    return data


def migrate_v1(old: dict) -> dict:
    """从 v1 state.json 平滑升级，保留全部历史事实。"""
    s = default_state()
    s["created_at"] = old.get("created_at", now_utc())
    s["known_repos"] = old.get("known_repos", {})
    s["repo_baseline"] = old.get("repo_baseline", {})
    s["upstream"] = old.get("upstream", {})
    s["auto_upstream"] = old.get("auto_upstream", {})
    s["heartbeat"]["last_success"] = old.get("last_check", "")
    s["heartbeat"]["last_attempt"] = old.get("last_check", "")
    # v1 的 notified_ids 是集合语义且被随机截断；这里逐条转成历史条目，一条不丢
    for i, eid in enumerate(old.get("notified_ids", [])):
        s["deliveries"].append({
            "id": str(eid), "ts": old.get("last_check", ""),
            "level": "legacy", "note": "从 v1 notified_ids 迁移，保留原 ID",
        })
    s["sync_decisions"].append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "note": "state v1 → v2 迁移：历史去重 ID 全量保留，未覆盖、未精简",
    })
    return s


def save(path: str, state: dict) -> None:
    """原子写，避免并发运行留下半截 JSON。

    防御：绝不把裸 v1 结构直接打上 version=2 写出。那样会产出
    「标着 v2、实际还是 v1」的半吊子文件，下次 load() 会跳过迁移、
    丢掉 notified_ids 的历史语义。任何来源的 v1 dict 都先就地迁移。
    """
    if "notified_ids" in state and state.get("version") != VERSION:
        state = migrate_v1(state)
    state["version"] = VERSION
    # 写盘前做隐私投影：state.json 会被 workflow commit 回公开仓库，
    # 里面绝不能留下 owner / 真实仓库名 / 上游来源。内存中的 state 不变。
    try:
        from . import privacy
        out = privacy.scrub_state(state)
    except Exception:
        out = state
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def is_notified(state: dict, event_id: str) -> bool:
    """已处理（含 pending，用于去重决策）。注意：不等于「已成功送达」。"""
    return is_delivered(state, event_id) or any(
        p.get("id") == event_id for p in state["pending"]
    )


def is_delivered(state: dict, event_id: str) -> bool:
    """真正推送成功才为 True。需要「成功才销账」的地方必须用它，

    不能用 is_notified —— 后者把 pending 也算进去，推送失败时会提前销账。
    """
    return any(d.get("id") == event_id for d in state["deliveries"])


def mark_notified(state: dict, event_id: str, level: str, title: str = "") -> None:
    """只在推送成功后调用。"""
    if is_notified(state, event_id):
        return
    state["deliveries"].append({
        "id": event_id, "ts": now_utc(), "level": level, "title": title[:120],
    })


def add_pending(state: dict, event_id: str, level: str, title: str, lines: list) -> None:
    if any(p.get("id") == event_id for p in state["pending"]):
        return
    state["pending"].append({
        "id": event_id, "ts": now_utc(), "level": level,
        "title": title, "lines": lines, "attempts": 0,
    })


def drop_pending(state: dict, event_id: str) -> None:
    state["pending"] = [p for p in state["pending"] if p.get("id") != event_id]


def prune(state: dict, keep_days: int = KEEP_DAYS, now: str | None = None) -> int:
    """按时间裁剪，不随机截断。返回被清理条数。"""
    ref = parse_ts(now or now_utc())
    if not ref:
        return 0
    # deliveries / lessons / sync_decisions 遵循只追加原则，不裁剪；
    # 仅清理 pending 中超过 7 天的死信，避免无限重试。
    n = 0
    fresh = []
    for p in state["pending"]:
        ts = parse_ts(p.get("ts", ""))
        if ts and ts < ref - timedelta(days=7):
            n += 1
        else:
            fresh.append(p)
    state["pending"] = fresh
    return n


def heartbeat_ok(state: dict, now: str | None = None) -> None:
    state["heartbeat"]["last_success"] = now or now_utc()
    state["heartbeat"]["last_attempt"] = now or now_utc()
    state["heartbeat"]["runs"] += 1
    state["heartbeat"]["failures"] = 0


def heartbeat_fail(state: dict, now: str | None = None) -> None:
    state["heartbeat"]["last_attempt"] = now or now_utc()
    state["heartbeat"]["failures"] += 1


def heartbeat_stale(state: dict, max_minutes: int, now: str | None = None) -> bool:
    last = parse_ts(state["heartbeat"].get("last_success", ""))
    if not last:
        return False  # 首次运行，只建基线不误报
    ref = parse_ts(now or now_utc()) or datetime.now(timezone.utc)
    return (ref - last).total_seconds() > max_minutes * 60


def add_lesson(state: dict, date: str, wrong: str, right: str, basis: str = "") -> None:
    """经验台账：只追加，禁止覆盖、禁止精简。"""
    state["lessons"].append({
        "date": date, "wrong": wrong, "right": right, "basis": basis, "ts": now_utc(),
    })


def add_todo(state: dict, todo_id: str, kind: str, repo: str, title: str,
             created_at: str, url: str = "") -> None:
    # 事件 ID 幂等：同 ID 无论是否已关闭都不重建。旧实现只判断「未关闭的同
    # ID」，销账后的待办一旦被重复处理就会复活，与「已关闭的不会被重建」矛盾。
    if any(t.get("id") == todo_id for t in state["todos"]):
        return
    state["todos"].append({
        "id": todo_id, "kind": kind, "repo": repo, "title": title,
        "created_at": created_at, "url": url, "closed": False,
    })


def close_todo(state: dict, todo_id: str, reason: str = "") -> bool:
    """销账：只追加关闭标记，绝不删除条目或改写原始字段（事实可追溯）。

    reason 可选，用于区分「本人回复已处理」与「自动结清历史噪声」，
    方便日后回看某条待办为什么被关掉。
    """
    for t in state["todos"]:
        if t.get("id") == todo_id and not t.get("closed"):
            t["closed"] = True
            t["closed_at"] = now_utc()
            if reason:
                t["close_reason"] = reason
            return True
    return False


def open_todos(state: dict) -> list:
    return [t for t in state["todos"] if not t.get("closed")]
