#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书推送（写白名单 W2）。

关键修复：只有推送成功才 mark_notified；失败进 pending 重试队列，
绝不「先记 ID 再说」导致事件永久丢失。P1/P2 在免打扰时段顺延，P0 照推。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from typing import Any

from . import state as st
from .digest import build_digest
from .report import Card

LEVEL_TAG = {"P0": "【P0】", "P1": "【P1】", "P2": "【P2】"}


def feishu_sign(timestamp: str, secret: str) -> str:
    """飞书自定义机器人「签名校验」算法（官方示例）。

    签名 = base64(HMAC-SHA256(key=f"{timestamp}\\n{secret}", msg=b""))
    开启校验后，payload 必须同时带 timestamp 与 sign，否则飞书拒收。
    """
    string_to_sign = "{}\n{}".format(timestamp, secret)
    hmac_code = hmac.new(string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def build_payload(title: str, lines: list, secret: str = "",
                  timestamp: str | None = None) -> dict:
    """构造飞书 post 消息；配置了 secret 时自动加签名。"""
    payload = {"msg_type": "post",
               "content": {"post": {"zh_cn": {
                   "title": title,
                   "content": [[{"tag": "text", "text": ln}] for ln in lines if ln],
               }}}}
    if secret:
        ts = timestamp or str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = feishu_sign(ts, secret)
    return payload


def build_card_payload(card: dict, secret: str = "",
                       timestamp: str | None = None) -> dict:
    """构造飞书 interactive 卡片消息（卡片 JSON 2.0）。

    自定义机器人 webhook 直接收 ``{"msg_type":"interactive","card":{...}}``，
    card 为内联 JSON，不需要搭建工具模板。签名算法与 post 完全一致
    （只依赖 timestamp + secret，与消息体无关），因此直接复用 feishu_sign。
    """
    payload = {"msg_type": "interactive", "card": card}
    if secret:
        ts = timestamp or str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = feishu_sign(ts, secret)
    return payload


class FeishuClient:
    def __init__(self, webhook: str = "", secret: str = "", dry_run: bool = False,
                 timeout: int = 20, verbose: bool = False):
        self.webhook = webhook or os.environ.get("FEISHU_WEBHOOK", "")
        # 可选：飞书机器人签名密钥。留空表示未开启签名校验（向后兼容）。
        self.secret = secret or os.environ.get("FEISHU_SECRET", "")
        self.dry_run = dry_run
        self.timeout = timeout
        self.verbose = verbose
        self.sent = 0
        self.failed = 0

    def _post(self, payload: dict) -> bool:
        """统一的出站请求。post / interactive 共用，失败一律返回 False 交给上层降级。"""
        # 显式 dry-run（离线回放 / 自检）：模拟成功，但绝不联网。
        if self.dry_run:
            if self.verbose:
                print(f"  [dry-run] 未实际推送：{payload.get('msg_type', '?')}")
            self.sent += 1
            return True
        # 真实运行却没配凭证：必须判失败。旧实现把这里当成成功，导致
        # 「代码已上线、Secrets 还没填」的空窗期内，事件被 mark_notified
        # 永久静默吞掉，用户配好 Webhook 后也再也收不到历史事件。
        if not self.webhook:
            self.failed += 1
            if self.verbose:
                print("  [warn] 未配置 FEISHU_WEBHOOK：本轮事件保持待发，不会记账")
            return False
        req = urllib.request.Request(
            self.webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read().decode("utf-8"))
                ok = resp.get("code") == 0 or resp.get("StatusCode") == 0
                if ok:
                    self.sent += 1
                else:
                    self.failed += 1
                    if resp.get("code") == 19021:
                        print("  [warn] 飞书签名校验失败（19021）：机器人已开启签名校验，"
                              "请确认 FEISHU_SECRET 已填写且与机器人一致、机器时钟正常")
                    elif resp.get("code") == 9499:
                        print("  [warn] 请求体格式错误（9499）：卡片 JSON 可能不被当前"
                              "机器人/客户端支持，本轮自动降级为富文本逐条推送")
                    else:
                        print(f"  [warn] 飞书返回非成功：{resp}")
                return ok
        except Exception as e:
            self.failed += 1
            print(f"  [warn] 飞书推送失败：{e}")
            return False

    def send(self, title: str, lines: list, level: str = "") -> bool:
        """逐条富文本（原有通道，降级时使用）。"""
        return self._post(build_payload(title, lines, self.secret))

    def send_card(self, card: dict) -> bool:
        """分类合并卡片（interactive / JSON 2.0）。失败由 deliver 自动降级。"""
        return self._post(build_card_payload(card, self.secret))


def should_skip_run(client: "FeishuClient", dry_run: bool,
                    replay: bool = False) -> bool:
    """凭证未就绪时整轮空转，不采集、不建基线、不推进水位、不写状态。

    真实场景：代码已经 push、但 Secrets 要过一会儿才填。这期间 cron 照跑，
    旧逻辑会照常建 BASELINE 基线并推进水位；等凭证配好，历史事件已被
    mark_notified 且水位越过，永远不会再被采集 —— 通知静默丢失。
    显式 --dry-run 与 --replay 是离线演练，不受影响。
    """
    return not dry_run and not replay and not client.webhook


def _in_quiet_hours(now: str, cfg: dict) -> bool:
    q = cfg.get("quiet_hours", {})
    if not q.get("enabled"):
        return False
    # now 为 UTC，配置按北京时间给，简单换算：+8 小时后取 HH:MM
    try:
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(hours=8)
        cur = dt.strftime("%H:%M")
        start, end = q.get("start", "23:30"), q.get("end", "08:00")
        return (cur >= start or cur <= end) if start > end else (start <= cur <= end)
    except Exception:
        return False


def deliver(state: dict, client: FeishuClient, cards: list[Card], cfg: dict,
            now: str, max_attempts: int = 5) -> dict:
    """交付所有卡片 + 重试上一轮 pending。返回统计。

    分类合并（QQ「合并转发」效果）：本轮多张卡片打成 **一张** interactive 卡片，
    按类别分折叠面板、P0 默认展开；群里一轮只多一条消息而不是几十条。
    只有整张卡真的送达，才把里面所有成员事件记账；卡片发不出去（旧客户端
    不支持 JSON 2.0 / 9499 / 网络抖动）自动降级为逐条富文本，绝不丢消息。

    每日在岗确认始终单独发：它是「必达」信号，pipeline 还要靠
    ``is_delivered(checkin.id)`` 决定当天是否已报到，不能并进汇总卡。
    """
    quiet = _in_quiet_hours(now, cfg)
    gcfg = cfg.get("grouping", {})
    merge_enabled = bool(gcfg.get("enabled", True))
    min_cards = int(gcfg.get("min_cards", 2))
    stats = {"sent": 0, "pending": 0, "deferred": 0, "retried": 0, "dead": 0,
             "merged": 0, "groups": 0, "digest_bytes": 0, "fallback": 0}

    # 1) 先重试上一轮失败的（最多 5 次，超过 7 天由 state.prune 清掉）
    remaining = []
    for p in list(state["pending"]):
        if p.get("attempts", 0) >= max_attempts:
            stats["dead"] += 1
            continue
        p["attempts"] = p.get("attempts", 0) + 1
        if client.send(p["title"], p.get("lines", []), p.get("level", "")):
            st.mark_notified(state, p["id"], p.get("level", ""), p.get("title", ""))
            st.drop_pending(state, p["id"])
            stats["retried"] += 1
        else:
            remaining.append(p)
    state["pending"] = remaining

    def _deliver_one(card: Card) -> bool:
        """逐条富文本通道。返回是否送达；失败方负责进 pending。"""
        if client.send(card.title, card.lines, card.level):
            for eid in card.event_ids:
                st.mark_notified(state, eid, card.level, card.title)
            st.mark_notified(state, card.id, card.level, card.title)
            st.drop_pending(state, card.id)
            stats["sent"] += 1
            return True
        st.add_pending(state, card.id, card.level, card.title, card.lines)
        stats["pending"] += 1
        return False

    # 2) 本轮分流：必达卡单独发；其余先按免打扰/去重筛一遍，再决定合并或逐条
    singles: list[Card] = []
    bucket: list[Card] = []
    for card in cards:
        # 每日在岗确认是「必达」信号：免打扰只压普通 P1/P2，不能压它，
        # 否则 quiet_hours 一开，恰恰在最需要确认的夜里收不到。
        must_deliver = card.group == "daily_checkin"
        if quiet and not must_deliver and card.level in ("P1", "P2"):
            st.add_pending(state, card.id, card.level, card.title, card.lines)
            stats["deferred"] += 1
            continue
        if st.is_notified(state, card.id):
            continue
        if must_deliver or not merge_enabled:
            singles.append(card)
        else:
            bucket.append(card)

    for card in singles:
        _deliver_one(card)

    # 卡片太少时没必要合并（也避免改变原有单条推送的形态）
    if len(bucket) < max(2, min_cards):
        for card in bucket:
            _deliver_one(card)
        return stats

    # 3) 合并转发：一张卡按类别折叠，P0 默认展开；内容过多时自动分片
    digest = build_digest(bucket, cfg, now)
    stats["groups"] = digest.groups
    stats["digest_bytes"] = digest.size_bytes
    stats["shards"] = digest.shards

    failed_shards: list[Card] = []
    for i, payload in enumerate(digest.payloads):
        if client.send_card(payload["card"]):
            # 只有这一片真送达，才给片内成员事件 + 成员卡 ID 记账。
            for card in digest.shard_members[i]:
                for eid in card.event_ids:
                    st.mark_notified(state, eid, card.level, card.title)
                st.mark_notified(state, card.id, card.level, card.title)
                st.drop_pending(state, card.id)
            stats["sent"] += 1
            stats["merged"] += len(digest.shard_members[i])
        else:
            # 单片失败不牵连其它片：只把这一片的成员降级逐条补发。
            failed_shards.extend(digest.shard_members[i])

    if failed_shards:
        stats["fallback"] = 1
        if client.verbose:
            print(f"  [warn] {len(failed_shards)} 张卡片降级为逐条富文本推送")
        for card in failed_shards:
            _deliver_one(card)
    return stats


def send_heartbeat_alert(client: FeishuClient, lines: list) -> bool:
    """心跳超时专用通道。旧实现标题写成「自身运行失败」，与工作流失败告警
    完全同名，把「只是 GitHub 调度慢了」误报成「程序挂了」，且是 P0。"""
    return client.send("【P1】管家心跳超时（久未巡检）", lines, "P1")
