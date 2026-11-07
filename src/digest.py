#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「合并转发」：把一轮的多张卡片打包成飞书卡片消息。

QQ 群「合并转发」在飞书里的等价物 = 少量 interactive 消息 + 按类别的折叠面板：

  * 群里一轮只多 1~3 条消息（而不是几十条平铺），顶部是总览；
  * 同类卡片归到同一个 ``collapsible_panel``，默认折叠，点开才看明细；
  * P0 / 心跳默认展开，要紧的事不被藏起来；
  * 严格控制在飞书自定义机器人 **20 KB 请求体上限**内：先逐级裁剪明细行，
    仍装不下就按优先级**自动分片**成多条卡片，绝不丢成员卡、绝不静默吞事件；
  * 每一片都如实标注「第 X/Y 片」与「另 N 行未展开」，不假装全量。

本模块只构造 JSON：不碰凭证、不发网络请求、不读写 state。
签名与投递在 ``notify.py``，记账语义仍由 ``deliver()`` 统一负责。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .report import Card

# 级别排序与展示。HEARTBEAT 与 P0 同级（都是「必须马上看见」）。
LEVEL_RANK = {"P0": 0, "HEARTBEAT": 0, "P1": 1, "P2": 2, "DROP": 3, "": 3}
LEVEL_ICON = {"P0": "🔴", "HEARTBEAT": "🔴", "P1": "🟠", "P2": "🟡", "DROP": "⚪", "": "⚪"}
HEADER_COLOR = {"P0": "red", "HEARTBEAT": "red", "P1": "orange", "P2": "blue", "DROP": "grey", "": "grey"}

# report.py 的 GROUP_LABEL 覆盖了业务类别，这里补齐系统类类别，避免面板标题出现英文 key。
GROUP_LABEL = {
    "build": "构建失败",
    "heartbeat": "管家自身 / 心跳",
    "daily_checkin": "每日在岗确认",
    "silence": "沉默与超期提醒",
    "human": "真人 Issue / 评论",
    "security": "安全告警",
    "notification": "通知中心",
    "daily": "例行日报",
    "upstream": "上游更新",
    "upstream_sensitive": "上游敏感更新（C/C++ / 构建 / 下载引擎）",
    "key_commit": "人工关键提交",
    "release": "Release 批量发布",
    "pr": "PR / 上游同步",
    "misc": "其它",
}

DEFAULT_MAX_BYTES = 18000   # 官方上限 20 KB，留 2 KB 余量给签名/timestamp 与转义
DEFAULT_MAX_LINES = 6       # 每张成员卡默认最多展示几行明细
DEFAULT_MAX_GROUPS = 12     # 折叠面板数量上限，超出的并进「其它」
MAX_TITLE_CHARS = 120       # 单条标题硬上限，防止一张卡就撑爆预算
MAX_LINE_CHARS = 300        # 单行明细硬上限


@dataclass
class Digest:
    """一次合并转发的结果。payload 不带签名，由 notify 统一补 timestamp/sign。"""
    payload: dict = field(default_factory=dict)      # 第一片（向后兼容）
    payloads: list = field(default_factory=list)     # 全部分片，按顺序送达
    shard_members: list = field(default_factory=list)  # 每片对应的成员卡，逐片独立记账
    member_ids: list = field(default_factory=list)   # 全量成员，记账用，不因分片而少
    groups: int = 0
    size_bytes: int = 0          # 最大单片的字节数
    max_lines: int = DEFAULT_MAX_LINES
    truncated: bool = False
    merged_groups: int = 0
    shards: int = 1

    def to_dict(self) -> dict:
        return {"groups": self.groups, "size_bytes": self.size_bytes,
                "max_lines": self.max_lines, "truncated": self.truncated,
                "merged_groups": self.merged_groups, "shards": self.shards,
                "member_count": len(self.member_ids)}


def _clip(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def _esc(s: str) -> str:
    """飞书卡片 markdown 的轻量转义，避免日志片段里的 * _ [ ` 被误解析。"""
    out = []
    for ch in str(s):
        if ch in "\\`*_[]":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _label(key: str) -> str:
    if key.startswith("level:"):
        lv = key.split(":", 1)[1]
        return f"{lv} 级消息"
    return GROUP_LABEL.get(key, key)


def _group_key(c: Card) -> str:
    """优先用业务类别；没有 group 时按级别兜底，保证「同类一定在一起」。"""
    if c.group:
        return c.group
    if "构建失败" in c.title:
        return "build"
    if "心跳" in c.title or "管家" in c.title:
        return "heartbeat"
    return "level:" + (c.level or "P2")


def _priority(c: Card) -> tuple:
    return (LEVEL_RANK.get(c.level, 3), _group_key(c), c.title)


def _buckets(cards: list) -> list:
    """返回 [(rank, key, members)]，先按级别再按类别名排序。"""
    b: dict[tuple[int, str], list] = {}
    for c in cards:
        rank = LEVEL_RANK.get(c.level, 3)
        b.setdefault((rank, _group_key(c)), []).append(c)
    out = []
    for (rank, key), members in b.items():
        members.sort(key=lambda x: (LEVEL_RANK.get(x.level, 3), x.title))
        out.append((rank, key, members))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def _render_member(c: Card, max_lines: int) -> str:
    """一张成员卡 → 一个 markdown 块。明细行数受 max_lines 约束。"""
    usable = [_clip(ln, MAX_LINE_CHARS) for ln in c.lines if ln and str(ln).strip()]
    shown = usable[:max_lines] if max_lines > 0 else []
    body = "\n".join("- " + _esc(ln) for ln in shown)
    hidden = len(usable) - len(shown)
    if hidden > 0:
        body += (("\n" if body else "") + f"- …另有 {hidden} 行明细未展开")
    head = "**" + _esc(_clip(c.title, MAX_TITLE_CHARS)) + "**"
    return head + ("\n" + body if body else "")


def _panel(rank: int, key: str, members: list, max_lines: int, idx: int) -> dict:
    lv = "P0" if rank == 0 else ("P1" if rank == 1 else "P2")
    label = _label(key)
    elements = [{"tag": "markdown", "content": _render_member(c, max_lines)}
                for c in members]
    return {
        "tag": "collapsible_panel",
        "element_id": f"grp_{idx}",   # 卡内唯一，字母开头，<=20 字符
        "expanded": rank == 0,        # P0 默认展开，要紧的不被藏
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"{LEVEL_ICON.get(lv, '⚪')} {label} · {len(members)} 项"},
            "vertical_align": "center",
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                     "color": "", "size": "16px 16px"},
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }


def _assemble(cards: list, max_lines: int, cfg: dict, now: str,
              shard_no: int = 1, shard_total: int = 1,
              total_cards: int = 0) -> tuple[dict, int, int]:
    """构造一张卡片 JSON 2.0。返回 (payload, 面板数, 被并入「其它」的组数)。"""
    gcfg = cfg.get("grouping", {})
    max_groups = int(gcfg.get("max_groups", DEFAULT_MAX_GROUPS))
    buckets = _buckets(cards)

    merged = 0
    if len(buckets) > max_groups:
        keep, rest = buckets[:max_groups - 1], buckets[max_groups - 1:]
        tail_cards = [c for _, _, ms in rest for c in ms]
        merged = len(rest)
        buckets = keep + [(2, "misc", tail_cards)]

    lv_counts: dict[str, int] = {}
    for c in cards:
        lv_counts[c.level] = lv_counts.get(c.level, 0) + 1
    highest = min((LEVEL_RANK.get(c.level, 3) for c in cards), default=3)
    color = {0: "red", 1: "orange", 2: "blue"}.get(highest, "grey")
    total_events = sum(len(c.event_ids) for c in cards)

    shard_note = (f"（第 {shard_no}/{shard_total} 片）" if shard_total > 1 else "")
    overview = [
        f"**巡检时间**：{_esc(_clip(now or '本轮', 40))}{shard_note}",
        f"**本轮汇总**：本片 {len(cards)} 项通知 / {total_events} 条事件 / 归为 {len(buckets)} 类"
        + (f"（全轮共 {total_cards} 项）" if shard_total > 1 else ""),
        "　".join(f"{LEVEL_ICON.get(k, '⚪')} {k} {v}" for k, v in sorted(
            lv_counts.items(), key=lambda kv: LEVEL_RANK.get(kv[0], 3))),
        "本轮只发这一组；同类已折叠，点开分类标题展开明细。" if shard_total == 1
        else "内容较多已自动分片；同类已折叠，点开分类标题展开明细。",
    ]

    elements: list[dict] = [{"tag": "markdown", "content": "\n".join(overview)},
                            {"tag": "hr"}]
    for i, (rank, key, members) in enumerate(buckets):
        elements.append(_panel(rank, key, members, max_lines, i))
    elements.append({"tag": "hr"})
    tail = "完整明细见 Actions 运行日志；本条为分类合并视图，不代表事件只有上面这些。"
    if shard_total > 1:
        tail += f"\n本轮共 {shard_total} 片，这是第 {shard_no} 片；各片都已正常记账，不会重复推送。"
    elements.append({"tag": "markdown", "content": tail})

    card = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": color,
            # 带级别标签：机器人若同时开了「自定义关键词」（如填 【P0】 或 【），
            # 卡片标题也能命中，不至于整条被 19024 拦下再走降级。
            "title": {"tag": "plain_text",
                      "content": ({0: "【P0】", 1: "【P1】", 2: "【P2】"}
                                  .get(highest, "【P2】") + "【仓库管家】本轮巡检汇总")},
        },
        "body": {"direction": "vertical", "padding": "12px 12px 12px 12px",
                 "elements": elements},
    }
    return {"msg_type": "interactive", "card": card}, len(buckets), merged


def _size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _best_fit(cards: list, cfg: dict, now: str, budget: int,
              start_lines: int, shard_no: int, shard_total: int,
              total_cards: int) -> tuple[dict, int, int, int, int]:
    """给一组卡片找最大可容纳的明细行数。返回 (payload, size, lines, groups, merged)。"""
    best = None
    for lines in (start_lines, 4, 2, 1, 0):
        p, groups, merged = _assemble(cards, lines, cfg, now,
                                      shard_no, shard_total, total_cards)
        size = _size(p)
        best = (p, size, lines, groups, merged)
        if size <= budget:
            break
    assert best is not None
    return best


def build_digest(cards: list, cfg: dict | None = None, now: str = "",
                 max_bytes: int | None = None) -> Digest:
    """把多张卡片合并成 1~N 条飞书卡片消息。

    硬约束：每片实测 UTF-8 字节数 <= 预算（官方 20 KB）。策略：

      1. 先尝试全部卡片装进一片，明细行数从 max_lines 逐级降到 0；
      2. 仍装不下 → 按优先级（P0 优先）贪心分片，每片再各自找最大行数；
      3. 单张卡本身超预算（极端长文本）→ 标题/每行已硬截断，单片必装得下。

    ``member_ids`` 始终是全量，绝不会因为「这一片放不下」而少记账。
    """
    cfg = cfg or {}
    gcfg = cfg.get("grouping", {})
    budget = int(max_bytes or gcfg.get("max_payload_bytes", DEFAULT_MAX_BYTES))
    start_lines = int(gcfg.get("max_lines_per_card", DEFAULT_MAX_LINES))

    if not cards:
        return Digest()

    ordered = sorted(cards, key=_priority)
    total = len(ordered)

    # 1) 全量一片
    p, size, lines, groups, merged = _best_fit(
        ordered, cfg, now, budget, start_lines, 1, 1, total)
    if size <= budget:
        return Digest(payload=p, payloads=[p], shard_members=[ordered],
                      member_ids=[c.id for c in cards],
                      groups=groups,
                      size_bytes=size, max_lines=lines,
                      truncated=(lines < start_lines or merged > 0 or
                                 any(len([x for x in c.lines if x]) > lines
                                     for c in ordered)),
                      merged_groups=merged, shards=1)

    # 2) 按优先级贪心分片：从最高级开始累加，放不下就切一片
    shards: list[list] = []
    cur: list = []
    for c in ordered:
        trial = cur + [c]
        _, sz, _, _, _ = _best_fit(trial, cfg, now, budget, start_lines,
                                   1, 1, total)
        if cur and sz > budget:
            shards.append(cur)
            cur = [c]
        else:
            cur = trial
    if cur:
        shards.append(cur)

    payloads = []
    max_size = 0
    min_lines = start_lines
    any_merged = 0
    max_groups = 0
    for i, shard in enumerate(shards, 1):
        p, size, lines, groups, merged = _best_fit(
            shard, cfg, now, budget, start_lines, i, len(shards), total)
        payloads.append(p)
        max_size = max(max_size, size)
        min_lines = min(min_lines, lines)
        any_merged += merged
        max_groups = max(max_groups, groups)
    return Digest(payload=payloads[0], payloads=payloads,
                  shard_members=shards,
                  member_ids=[c.id for c in cards],
                  groups=max_groups,
                  size_bytes=max_size, max_lines=min_lines,
                  truncated=True, merged_groups=any_merged,
                  shards=len(shards))
