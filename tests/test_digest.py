#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分类合并转发（QQ「合并转发」效果）的单元测试。

覆盖：分类折叠、P0 默认展开、20KB 预算裁剪、签名、合并送达记账、
卡片失败自动降级、每日在岗确认不并入、二次幂等、开关回退。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import state as st  # noqa: E402
from src.digest import (build_digest, GROUP_LABEL,  # noqa: E402
                        _buckets, _group_key)
from src.notify import (FeishuClient, build_card_payload, deliver,  # noqa: E402
                        feishu_sign)
from src.report import Card  # noqa: E402

with open(os.path.join(ROOT, "config/settings.json"), encoding="utf-8") as _f:
    CFG = json.load(_f)

NOW = "2026-11-06T09:14:56Z"


def make_cards(n: int = 6) -> list:
    """构造一组跨级别、跨类别的卡片。"""
    return [
        Card("card:p0:build:1", "P0", "【P0】构建失败 · app-primary（3 次）",
             ["仓库：your-org/app-primary", "错误归类：链接错误"],
             ["run:app-primary:1", "run:app-primary:2"], "build"),
        Card("card:p0:issue:1", "P0", "【P0】your-org/app-backup · issue",
             ["仓库：your-org/app-backup", "标题：能编译吗"], ["issue:app-backup:9"], "human"),
        Card("card:p1:key:1", "P1", "【P1】人工关键提交（5 条）",
             ["· app-primary：修复下载引擎"], ["commit:app-primary:a"], "key_commit"),
        Card("card:p1:release:1", "P1", "【P1】Release 批量发布（4 条）",
             ["· app-file-manager v1.2.1"], ["release:sample:1"], "release"),
        Card("card:p2:daily:1", "P2", "【P2】例行日报",
             ["本轮共 100 条例行事件（不逐条推）："], ["push:x:1"], "daily"),
        Card("card:silence:1", "P1", "【P1】待办已超期",
             ["3 件待办已超期："], ["silence:overdue:2026-11-06"], "silence"),
    ][:n]


class TestDigestStructure(unittest.TestCase):
    def test_msg_type_is_interactive_and_schema_2(self):
        d = build_digest(make_cards(), CFG, NOW)
        self.assertEqual(d.payload["msg_type"], "interactive")
        self.assertEqual(d.payload["card"]["schema"], "2.0")

    def test_groups_are_classified_not_flat(self):
        cards = make_cards()
        d = build_digest(cards, CFG, NOW)
        panels = [e for e in d.payload["card"]["body"]["elements"]
                  if e.get("tag") == "collapsible_panel"]
        self.assertGreaterEqual(len(panels), 4, "同类应归到不同折叠面板")
        self.assertEqual(d.groups, len(panels))
        self.assertEqual(len(d.member_ids), len(cards))

    def test_same_class_lands_in_one_panel(self):
        cards = make_cards(2) + [
            Card("card:p0:build:2", "P0", "【P0】构建失败 · lib-render（2 次）",
                 ["仓库：your-org/lib-render"], ["run:lib-render:1"], "build"),
        ]
        b = _buckets(cards)
        build_bucket = [x for x in b if x[1] == "build"]
        self.assertEqual(len(build_bucket), 1, "两个构建失败必须在同一个面板")
        self.assertEqual(len(build_bucket[0][2]), 2)

    def test_p0_expanded_p2_collapsed(self):
        d = build_digest(make_cards(), CFG, NOW)
        panels = [e for e in d.payload["card"]["body"]["elements"]
                  if e.get("tag") == "collapsible_panel"]
        p0 = [p for p in panels if "🔴" in p["header"]["title"]["content"]]
        p2 = [p for p in panels if "🟡" in p["header"]["title"]["content"]]
        self.assertTrue(p0 and all(p["expanded"] for p in p0), "P0 必须默认展开")
        self.assertTrue(p2 and not any(p["expanded"] for p in p2), "P2 默认折叠")

    def test_header_color_follows_highest_level(self):
        d = build_digest(make_cards(), CFG, NOW)
        self.assertEqual(d.payload["card"]["header"]["template"], "red")
        only_p2 = build_digest([make_cards(5)[4]], CFG, NOW)
        self.assertEqual(only_p2.payload["card"]["header"]["template"], "blue")

    def test_element_ids_unique(self):
        cards = make_cards()
        # 造出超过 max_groups 的类别，验证合并不产生重复 element_id
        extra = [Card(f"card:g{i}", "P2", f"【P2】类别{i}", ["行"],
                      [f"e{i}"], f"group{i}") for i in range(20)]
        d = build_digest(cards + extra, CFG, NOW)
        ids = [e.get("element_id") for e in d.payload["card"]["body"]["elements"]
               if e.get("tag") == "collapsible_panel"]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual(d.groups, CFG["grouping"]["max_groups"])

    def test_overview_counts_levels(self):
        d = build_digest(make_cards(), CFG, NOW)
        overview = d.payload["card"]["body"]["elements"][0]["content"]
        self.assertIn("6 项通知", overview)
        self.assertIn("P0", overview)
        self.assertIn("点开分类标题展开明细", overview)

    def test_special_chars_escaped(self):
        c = Card("c1", "P0", "【P0】标题*带[特殊]_字符", ["`code` *bold* [x]"], ["e1"], "build")
        d = build_digest([c, make_cards(1)[0]], CFG, NOW)
        # 转义发生在 payload 的字符串值里；json.dumps 会把反斜杠再转义一层
        panels = [e for e in d.payload["card"]["body"]["elements"]
                  if e.get("tag") == "collapsible_panel"]
        texts = " ".join(x["content"] for p in panels for x in p["elements"])
        self.assertIn(r"\*bold\*", texts)
        self.assertIn(r"\[x\]", texts)


class TestBudget(unittest.TestCase):
    def test_under_20k_limit(self):
        d = build_digest(make_cards(), CFG, NOW)
        self.assertLessEqual(d.size_bytes, CFG["grouping"]["max_payload_bytes"])
        payload_bytes = len(json.dumps(
            build_card_payload(d.payload["card"], "sec", "1700000000"),
            ensure_ascii=False).encode("utf-8"))
        self.assertLess(payload_bytes, 20 * 1024, "含签名也必须低于官方 20KB 上限")

    def test_huge_content_is_trimmed_not_dropped(self):
        cards = [Card(f"c{i}", "P2", f"【P2】类别{i}",
                      [("很长的一行日志内容 " * 40) for _ in range(30)],
                      [f"e{i}"], f"g{i}") for i in range(15)]
        cfg = copy.deepcopy(CFG)
        cfg["grouping"] = dict(cfg["grouping"], max_payload_bytes=6000)
        d = build_digest(cards, cfg, NOW)
        self.assertLessEqual(d.size_bytes, 6000)
        self.assertTrue(d.truncated, "裁剪必须如实标记")
        self.assertEqual(len(d.member_ids), len(cards), "裁剪明细不能丢成员卡记账")
        body = json.dumps(d.payload, ensure_ascii=False)
        self.assertIn("未展开", body, "必须明说另有明细未展开")

    def test_single_card_digest_still_valid(self):
        d = build_digest([make_cards(2)[0]], CFG, NOW)
        self.assertEqual(d.groups, 1)
        self.assertGreater(d.size_bytes, 0)


class TestCardPayload(unittest.TestCase):
    def test_card_payload_without_secret(self):
        d = build_digest(make_cards(), CFG, NOW)
        p = build_card_payload(d.payload["card"])
        self.assertEqual(p["msg_type"], "interactive")
        self.assertNotIn("sign", p)
        self.assertNotIn("timestamp", p)

    def test_card_payload_sign_same_algorithm_as_post(self):
        d = build_digest(make_cards(), CFG, NOW)
        p = build_card_payload(d.payload["card"], "sec", "1700000000")
        self.assertEqual(p["sign"], feishu_sign("1700000000", "sec"))
        self.assertEqual(p["timestamp"], "1700000000")


class _FakeResponse:
    """模拟 urlopen 的上下文管理器。"""
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client_with_response(body: dict, **kw) -> FeishuClient:
    c = FeishuClient(webhook="https://example.invalid/hook", **kw)
    return c


class TestDeliverMerge(unittest.TestCase):
    def test_multiple_cards_become_one_message(self):
        s = st.default_state()
        cards = make_cards()
        c = FeishuClient(dry_run=True)
        stats = deliver(s, c, cards, CFG, NOW)
        self.assertEqual(stats["sent"], 1, "一轮只发一条合并卡")
        self.assertEqual(stats["merged"], len(cards))
        self.assertGreaterEqual(stats["groups"], 4)
        self.assertEqual(stats["fallback"], 0)
        # 所有成员事件 + 成员卡 ID 都只在送达后记账
        for card in cards:
            self.assertTrue(st.is_delivered(s, card.id))
            for eid in card.event_ids:
                self.assertTrue(st.is_delivered(s, eid))

    def test_second_run_is_idempotent(self):
        s = st.default_state()
        first = deliver(s, FeishuClient(dry_run=True), make_cards(), CFG, NOW)
        second = deliver(s, FeishuClient(dry_run=True), make_cards(), CFG, NOW)
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0, "同一轮卡片不能重复推")

    def test_daily_checkin_stays_separate(self):
        s = st.default_state()
        cards = make_cards() + [
            Card("card:daily:checkin:2026-11-06", "P2", "【每日在岗】repo-monitor 正常",
                 ["本轮巡检正常完成"], ["daily:checkin:2026-11-06"], "daily_checkin"),
        ]
        stats = deliver(s, FeishuClient(dry_run=True), cards, CFG, NOW)
        self.assertEqual(stats["sent"], 2, "汇总卡 1 条 + 在岗确认 1 条")
        self.assertEqual(stats["merged"], len(cards) - 1, "在岗卡不能并进汇总")
        self.assertTrue(st.is_delivered(s, "daily:checkin:2026-11-06"))

    def test_card_failure_falls_back_to_plain_posts(self):
        s = st.default_state()
        cards = make_cards()
        c = _client_with_response({"code": 9499, "msg": "Bad Request"})
        # 卡片请求返回 9499，随后的逐条 post 返回成功
        responses = [_FakeResponse({"code": 9499, "msg": "Bad Request"})] + \
                    [_FakeResponse({"code": 0}) for _ in cards]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            stats = deliver(s, c, cards, CFG, NOW)
        self.assertEqual(stats["fallback"], 1)
        self.assertEqual(stats["sent"], len(cards), "降级后逐条全部补发")
        self.assertEqual(stats["pending"], 0, "降级成功不能留在 pending")
        for card in cards:
            self.assertTrue(st.is_delivered(s, card.id))

    def test_merge_failure_and_fallback_failure_go_to_pending(self):
        s = st.default_state()
        cards = make_cards(3)
        c = _client_with_response({"code": 0})
        with mock.patch("urllib.request.urlopen",
                        side_effect=RuntimeError("network down")):
            stats = deliver(s, c, cards, CFG, NOW)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(stats["pending"], len(cards))
        for card in cards:
            self.assertFalse(st.is_delivered(s, card.id), "没送达绝不记账")

    def test_no_credentials_never_marks_delivered(self):
        s = st.default_state()
        cards = make_cards()
        with mock.patch.dict(os.environ, {"FEISHU_WEBHOOK": "", "FEISHU_SECRET": ""},
                             clear=False):
            c = FeishuClient()
            stats = deliver(s, c, cards, CFG, NOW)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(stats["pending"], len(cards))
        self.assertFalse(st.is_delivered(s, cards[0].event_ids[0]))

    def test_disabled_grouping_uses_legacy_path(self):
        s = st.default_state()
        cfg = copy.deepcopy(CFG)
        cfg["grouping"] = dict(cfg["grouping"], enabled=False)
        cards = make_cards()
        stats = deliver(s, FeishuClient(dry_run=True), cards, cfg, NOW)
        self.assertEqual(stats["sent"], len(cards), "逐条推送")
        self.assertEqual(stats["merged"], 0)

    def test_single_card_not_merged(self):
        s = st.default_state()
        stats = deliver(s, FeishuClient(dry_run=True), [make_cards(2)[0]], CFG, NOW)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["merged"], 0)

    def test_quiet_hours_defers_p1_p2_but_keeps_checkin(self):
        s = st.default_state()
        cfg = copy.deepcopy(CFG)
        cfg["quiet_hours"] = {"enabled": True, "start": "23:30", "end": "08:00",
                              "tz": "+08:00"}
        # 北京时间 23:45 = UTC 15:45，落在免打扰时段
        now = "2026-11-05T15:45:00Z"
        cards = make_cards() + [
            Card("card:daily:checkin:2026-11-06", "P2", "【每日在岗】repo-monitor 正常",
                 ["本轮巡检正常完成"], ["daily:checkin:2026-11-06"], "daily_checkin"),
        ]
        stats = deliver(s, FeishuClient(dry_run=True), cards, cfg, now)
        # P1/P2 顺延；P0 两张仍可合并；在岗确认必达
        self.assertGreater(stats["deferred"], 0)
        self.assertTrue(st.is_delivered(s, "daily:checkin:2026-11-06"))


class TestDigestDiagnostics(unittest.TestCase):
    def test_to_dict_exposes_budget_info(self):
        d = build_digest(make_cards(), CFG, NOW)
        info = d.to_dict()
        for key in ("groups", "size_bytes", "max_lines", "truncated",
                    "merged_groups", "member_count"):
            self.assertIn(key, info)

    def test_group_labels_are_chinese(self):
        for key in ("build", "heartbeat", "daily_checkin", "silence", "release"):
            self.assertFalse(GROUP_LABEL[key].isascii(), f"{key} 应有中文名")
        self.assertEqual(_group_key(Card("c", "P2", "【P2】x", [], [], "")), "level:P2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
