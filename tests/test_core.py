#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import gh, state as st  # noqa: E402
from src.events import (Event, ReplaySource, WebhookSource,  # noqa: E402
                        _upstream_pairs, parse_upstream_compare)
from src.notify import (FeishuClient, build_payload, deliver,  # noqa: E402
                        feishu_sign, should_skip_run)
from src.report import (build_cards, checkin_day, daily_checkin_card,  # noqa: E402
                        summarize_error)
from src.rules import Decision, classify, files_status, hits_sensitive  # noqa: E402

with open(os.path.join(ROOT, "config/settings.json"), encoding="utf-8") as _f:
    CFG = json.load(_f)
with open(os.path.join(ROOT, "config/rules.json"), encoding="utf-8") as _f:
    RULES = json.load(_f)


class TestReadonly(unittest.TestCase):
    def test_get_allowed(self):
        gh.assert_readonly("GET")

    def test_post_blocked(self):
        with self.assertRaises(gh.ReadOnlyViolation):
            gh.assert_readonly("POST")

    def test_put_blocked(self):
        with self.assertRaises(gh.ReadOnlyViolation):
            gh.assert_readonly("PUT")

    def test_static_guard(self):
        import subprocess
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tools/check_readonly.py")],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class TestClassify(unittest.TestCase):
    def test_human_issue_is_p0(self):
        e = Event("issue:your-org/app-primary:18", "issue", "your-org/app-primary",
                  "contributor-b", "2026-11-03T14:39:23Z", title="下载失败")
        d = classify(e, CFG, RULES)
        self.assertEqual(d.level, "P0")
        self.assertIn("下载", d.advice)

    def test_coderabbit_comment_dropped(self):
        e = Event("comment:x:1", "comment", "your-org/app-file-manager", "coderabbitai[bot]",
                  "2026-11-01T00:00:00Z",
                  body="<!-- This is an auto-generated comment: summarize by coderabbit.ai -->")
        self.assertEqual(classify(e, CFG, RULES).level, "DROP")

    def test_owner_comment_is_not_external(self):
        e = Event("comment:x:2", "comment", "your-org/app-backup", "your-org",
                  "2026-09-20T00:00:00Z", body="你指的或许是 upstream-org 开发者的项目")
        d = classify(e, CFG, RULES)
        self.assertEqual(d.level, "P2")
        self.assertTrue(d.meta.get("closes_todo"))

    def test_chore_bot_push_is_p2(self):
        e = Event("push:x:1", "push", "your-org/component-core",
                  "github-actions[bot]", "2026-11-05T10:00:00Z",
                  title="chore: sync SampleNative nightly")
        self.assertEqual(classify(e, CFG, RULES).level, "P2")

    def test_sensitive_push_is_p1(self):
        e = Event("push:x:2", "push", "your-org/app-primary", "your-org",
                  "2026-11-05T10:00:00Z", title="fix: 下载引擎握手重试",
                  meta={"files": ["src/main/cpp/download/engine.c"]})
        d = classify(e, CFG, RULES)
        self.assertEqual(d.level, "P1")
        self.assertIn("真机验证", d.advice)

    def test_self_failure_goes_to_heartbeat(self):
        e = Event("run:your-org/repo-monitor:1", "run_failure",
                  "your-org/repo-monitor", "your-org", "2026-11-05T10:00:00Z")
        self.assertEqual(classify(e, CFG, RULES).level, "HEARTBEAT")

    def test_real_failure_is_p0(self):
        e = Event("run:your-org/app-primary:2", "run_failure", "your-org/app-primary",
                  "your-org", "2026-11-05T10:00:00Z")
        self.assertEqual(classify(e, CFG, RULES).level, "P0")


class TestReport(unittest.TestCase):
    def test_error_link_category(self):
        log = "\n".join(["0 errors, 0 warnings",
                         "ld: error: undefined reference to 'dl_engine_init'",
                         "##[error]Process completed with exit code 1."])
        r = summarize_error(log, RULES)
        self.assertEqual(r["category"], "link")
        self.assertNotIn("0 errors", r["snippet"])

    def test_error_permission_category(self):
        r = summarize_error("remote: Permission denied (publickey)\nfatal: Could not read", RULES)
        self.assertEqual(r["category"], "permission")

    def test_build_failures_merged_per_repo(self):
        ds = [Decision(f"run:x:{i}", "run_failure", "your-org/app-primary", "your-org",
                       "Build", "P0", reason="构建失败", group="build",
                       meta={"workflow": "Build", "branch": "main"})
              for i in range(4)]
        cards = build_cards(ds, CFG, RULES, now="2026-11-05T12:00:00Z")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].level, "P0")
        self.assertEqual(len(cards[0].event_ids), 4)

    def test_release_batch_single_card(self):
        ds = [Decision(f"rel:{i}", "release", "your-org/plugin-hud", "your-org",
                       f"v1.{i}", "P1", group="release") for i in range(5)]
        cards = build_cards(ds, CFG, RULES, now="2026-11-05T12:00:00Z")
        self.assertEqual(len(cards), 1)
        self.assertIn("5 条", cards[0].title)


class TestState(unittest.TestCase):
    def test_v1_migration_keeps_everything(self):
        v1 = {"last_check": "2026-10-29T09:33:00Z",
              "notified_ids": ["a", "b", "c", "d"],
              "known_repos": {"your-org/app-primary": {"fork": False}},
              "upstream": {"your-org/app-file-manager": "abc1234"},
              "repo_baseline": {}, "auto_upstream": {}}
        v2 = st.migrate_v1(v1)
        self.assertEqual(len(v2["deliveries"]), 4)
        self.assertEqual(v2["known_repos"], v1["known_repos"])
        self.assertEqual(v2["upstream"], v1["upstream"])
        self.assertEqual(v2["version"], 2)

    def test_only_mark_after_success(self):
        s = st.default_state()
        self.assertFalse(st.is_notified(s, "e1"))
        st.add_pending(s, "e1", "P0", "t", ["a"])
        self.assertFalse(any(d["id"] == "e1" for d in s["deliveries"]))
        st.drop_pending(s, "e1")
        st.mark_notified(s, "e1", "P0", "t")
        self.assertTrue(st.is_notified(s, "e1"))

    def test_deliveries_append_only(self):
        s = st.default_state()
        for i in range(10):
            st.mark_notified(s, f"e{i}", "P2", "")
        n = len(s["deliveries"])
        st.prune(s)
        self.assertEqual(len(s["deliveries"]), n, "事件日志只追加，禁止随机截断")

    def test_heartbeat_stale(self):
        s = st.default_state()
        s["heartbeat"]["last_success"] = "2026-11-05T08:00:00Z"
        self.assertTrue(st.heartbeat_stale(s, 90, "2026-11-05T10:00:00Z"))
        self.assertFalse(st.heartbeat_stale(s, 90, "2026-11-05T09:00:00Z"))

    def test_pending_is_not_delivered(self):
        """推送失败进 pending 时，不能算『已成功送达』，否则日报会提前销账。"""
        s = st.default_state()
        st.add_pending(s, "card:daily:checkin:2026-11-05", "P2", "在岗", ["x"])
        self.assertTrue(st.is_notified(s, "card:daily:checkin:2026-11-05"))
        self.assertFalse(st.is_delivered(s, "card:daily:checkin:2026-11-05"))
        st.drop_pending(s, "card:daily:checkin:2026-11-05")
        st.mark_notified(s, "card:daily:checkin:2026-11-05", "P2", "在岗")
        self.assertTrue(st.is_delivered(s, "card:daily:checkin:2026-11-05"))


class TestDailyCheckin(unittest.TestCase):
    def test_one_per_beijing_day(self):
        s = st.default_state()
        now = "2026-11-05T12:00:00Z"  # 北京时间 11-05 20:00
        c1 = daily_checkin_card(s, CFG, now, event_count=10, upstream_count=2)
        self.assertIsNotNone(c1)
        self.assertEqual(c1.group, "daily_checkin")
        self.assertIn("2026-11-05", c1.title + " ".join(c1.lines))
        s["daily"]["last_report"] = "2026-11-05"
        self.assertIsNone(daily_checkin_card(s, CFG, now))
        # 跨到北京时间次日就应重新发
        s["daily"]["last_report"] = "2026-11-04"
        self.assertIsNotNone(daily_checkin_card(s, CFG, now))

    def test_utc_midnight_uses_beijing_day(self):
        # UTC 11-04 16:00 = 北京 11-05 00:00，不能按 UTC 日期判重
        self.assertEqual(checkin_day("2026-11-04T16:00:00Z"), "2026-11-05")

    def test_pending_does_not_mark_checkin_delivered(self):
        """失败进 pending 时 last_report 不写，下一轮应能补发。"""
        s = st.default_state()
        now = "2026-11-05T12:00:00Z"
        card = daily_checkin_card(s, CFG, now)
        st.add_pending(s, card.id, card.level, card.title, card.lines)
        # pipeline 用的是 is_delivered，pending 不算
        self.assertFalse(st.is_delivered(s, card.id))
        self.assertEqual(s["daily"]["last_report"], "")

    def test_quiet_hours_does_not_defer_checkin(self):
        s = st.default_state()
        cfg = dict(CFG)
        cfg["quiet_hours"] = {"enabled": True, "start": "23:30", "end": "08:00",
                              "tz": "+08:00"}
        # 北京时间 23:45 = UTC 15:45，落在免打扰时段
        now = "2026-11-05T15:45:00Z"
        card = daily_checkin_card(s, cfg, now)
        stats = deliver(s, FeishuClient(dry_run=True), [card], cfg, now)
        self.assertEqual(stats["deferred"], 0, "在岗确认不能被免打扰顺延")
        self.assertEqual(stats["sent"], 1)


class TestWebhook(unittest.TestCase):
    def test_push_carries_files(self):
        wh = WebhookSource("push", {
            "repository": {"full_name": "your-org/app-primary"},
            "sender": {"login": "your-org"}, "pusher": {"name": "your-org"},
            "after": "abc123", "ref": "refs/heads/main",
            "head_commit": {"timestamp": "2026-11-05T10:00:00Z"},
            "commits": [{"message": "fix: engine", "modified": ["src/main/cpp/engine.c"]}],
        }, delivery_id="d-1")
        evs = wh.collect(st.default_state(), CFG)
        self.assertEqual(len(evs), 1)
        self.assertIn("src/main/cpp/engine.c", evs[0].meta["files"])
        self.assertEqual(classify(evs[0], CFG, RULES).level, "P1")

    def test_delivery_id_is_idempotency_key(self):
        a = WebhookSource("issues", {"repository": {"full_name": "r"},
                                     "sender": {"login": "u"}, "action": "opened",
                                     "issue": {"number": 1, "title": "t"}},
                          delivery_id="same")
        b = WebhookSource("issues", {"repository": {"full_name": "r"},
                                     "sender": {"login": "u"}, "action": "opened",
                                     "issue": {"number": 1, "title": "t"}},
                          delivery_id="same")
        self.assertEqual(a.collect({}, CFG)[0].id, b.collect({}, CFG)[0].id)


class TestFixture(unittest.TestCase):
    def test_fixture_loads(self):
        fx = ReplaySource(os.path.join(ROOT, "tests/fixtures/events-sample.json"))
        self.assertGreaterEqual(len(fx.events), 600)
        ids = [e.id for e in fx.events]
        self.assertEqual(len(ids), len(set(ids)), "fixture 已完成语义 ID 去重")


class TestMissingCredentials(unittest.TestCase):
    """代码已上线但 Secrets 还没填的空窗期：绝不把事件误记成「已送达」。

    真实场景：用户先在网页建好机器人、代码先 push 了，但 Secrets 要隔一会儿
    才填。这期间 cron 照跑；若把「没配 Webhook」当成成功，事件会被
    mark_notified 永久静默吞掉，之后再也不会补发。
    """

    def _env(self):
        from unittest import mock
        return mock.patch.dict(os.environ,
                               {"FEISHU_WEBHOOK": "", "FEISHU_SECRET": ""},
                               clear=False)

    def test_no_webhook_send_fails(self):
        with self._env():
            c = FeishuClient(dry_run=False)
            self.assertFalse(c.send("标题", ["正文"]))
            self.assertEqual(c.sent, 0)
            self.assertEqual(c.failed, 1)

    def test_no_webhook_never_marks_delivered(self):
        from src.report import Card
        with self._env():
            s = st.default_state()
            card = Card("card:test:1", "P0", "标题", ["正文"], ["event:1"], "test")
            stats = deliver(s, FeishuClient(dry_run=False), [card], CFG,
                            "2026-11-05T10:00:00Z")
            self.assertEqual(stats["sent"], 0)
            self.assertFalse(st.is_delivered(s, "event:1"),
                             "没配凭证时事件不能被记为已送达")
            self.assertTrue(any(p["id"] == "card:test:1" for p in s["pending"]),
                            "应留在 pending，配好凭证后自动补发")

    def test_explicit_dry_run_still_succeeds(self):
        """离线回放 / 自检用的显式 dry-run 不受影响。"""
        with self._env():
            c = FeishuClient(dry_run=True)
            self.assertTrue(c.send("标题", ["正文"]))
            self.assertEqual(c.sent, 1)

    def test_skip_when_no_credentials(self):
        """真实运行 + 没配 Webhook → 整轮空转，保护水位与基线。"""
        with self._env():
            c = FeishuClient(dry_run=False)
            self.assertTrue(should_skip_run(c, dry_run=False))

    def test_no_skip_for_dry_run_or_replay(self):
        with self._env():
            c = FeishuClient(dry_run=False)
            self.assertFalse(should_skip_run(c, dry_run=True), "显式 dry-run 不受影响")
            self.assertFalse(should_skip_run(c, dry_run=False, replay=True),
                             "离线回放不受影响")

    def test_no_skip_when_webhook_present(self):
        c = FeishuClient(webhook="https://example.invalid/hook", dry_run=False)
        self.assertFalse(should_skip_run(c, dry_run=False))

    def test_sign_fail_response_is_not_success(self):
        """飞书签名校验失败返回 HTTP 200 + code 19021，不能算成功。"""
        from unittest import mock
        import io
        fake = mock.Mock()
        fake.read.return_value = json.dumps(
            {"code": 19021, "msg": "sign match fail"}).encode("utf-8")
        fake.__enter__ = mock.Mock(return_value=fake)
        fake.__exit__ = mock.Mock(return_value=False)
        c = FeishuClient(webhook="https://example.invalid/hook")
        with mock.patch("urllib.request.urlopen", return_value=fake):
            self.assertFalse(c.send("标题", ["正文"]))
        self.assertEqual(c.failed, 1)
        self.assertEqual(c.sent, 0)


class TestFeishuSignature(unittest.TestCase):
    """飞书签名校验：三条通道（主程序 / Worker / 失败告警）算法必须一致。"""

    def test_official_vector(self):
        # 按飞书官方算法独立复算，避免自证
        import base64, hashlib, hmac
        ts, secret = "1700000000", "test-secret"
        expected = base64.b64encode(hmac.new(
            ("%s\n%s" % (ts, secret)).encode("utf-8"),
            digestmod=hashlib.sha256).digest()).decode("utf-8")
        self.assertEqual(feishu_sign(ts, secret), expected)

    def test_payload_without_secret_is_plain(self):
        p = build_payload("t", ["a", "b"])
        self.assertEqual(p["msg_type"], "post")
        self.assertNotIn("sign", p)
        self.assertNotIn("timestamp", p)

    def test_payload_with_secret_has_sign(self):
        p = build_payload("t", ["a"], secret="sec", timestamp="1700000000")
        self.assertEqual(p["timestamp"], "1700000000")
        self.assertEqual(p["sign"], feishu_sign("1700000000", "sec"))

    def test_sign_changes_with_timestamp(self):
        self.assertNotEqual(feishu_sign("1700000000", "s"),
                            feishu_sign("1700000001", "s"))

    def test_sign_changes_with_secret(self):
        self.assertNotEqual(feishu_sign("1700000000", "s1"),
                            feishu_sign("1700000000", "s2"))


class TestUpstream(unittest.TestCase):
    """上游雷达：compare 解析、文件清单三态、敏感路径、bot 不被误吞。

    这些用例对应之前的真空区：主回放 fixture 里 0 条 upstream 事件，
    上游链路从来没被回归验证过，注释却写着「compare」。
    """

    @classmethod
    def setUpClass(cls):
        cls.fx = ReplaySource(os.path.join(
            ROOT, "tests/fixtures/upstream-sample.json"))
        cls.by_id = {e.id: e for e in cls.fx.events}

    def test_fixture_has_upstream_events(self):
        self.assertEqual(len(self.fx.events), 5)
        self.assertTrue(all(e.kind == "upstream" for e in self.fx.events))
        self.assertIn("upstream", CFG)

    def test_compare_parse_normal(self):
        raw = {"ahead_by": 2, "behind_by": 240, "total_commits": 240,
               "status": "behind", "commits": [{"sha": "x"}] * 240,
               "files": [{"filename": "module/driver_a.c"}]}
        m = parse_upstream_compare(raw)
        self.assertEqual(m["compare_status"], "ok")
        self.assertEqual(m["behind_by"], 240)
        self.assertEqual(m["files"], ["module/driver_a.c"])
        self.assertFalse(m["files_truncated"])
        self.assertEqual(m["files_status"], "ok")

    def test_compare_parse_detects_truncation(self):
        # total_commits > len(commits)：GitHub 截断了 commits，files 也可能不全
        m = parse_upstream_compare({"ahead_by": 0, "behind_by": 310,
                                    "total_commits": 310, "status": "behind",
                                    "commits": [{"sha": "x"}] * 250,
                                    "files": [{"filename": "README.md"}]})
        self.assertTrue(m["files_truncated"])

    def test_compare_parse_rejects_non_compare_response(self):
        self.assertEqual(parse_upstream_compare(None), {})
        self.assertEqual(parse_upstream_compare({"message": "Not Found"}), {})
        self.assertEqual(parse_upstream_compare([]), {})

    def test_files_status_three_states(self):
        e1 = Event("u1", "upstream", "r", meta={"files": ["a.c"], "files_status": "ok"})
        e2 = Event("u2", "upstream", "r", meta={"files": ["a.c"],
                                                "files_truncated": True, "files_status": "ok"})
        e3 = Event("u3", "upstream", "r", meta={"files": [],
                                                "files_status": "unavailable"})
        e4 = Event("u4", "upstream", "r", meta={})  # 老数据，连标记都没有
        self.assertEqual(files_status(e1), "ok")
        self.assertEqual(files_status(e2), "truncated")
        self.assertEqual(files_status(e3), "unavailable")
        self.assertEqual(files_status(e4), "unavailable")

    def test_sensitive_upstream_is_p1_with_advice(self):
        e = self.by_id["upstream:your-org/app-droid-ui:d1bd23d0001"]
        d = classify(e, CFG, RULES)
        self.assertEqual(d.level, "P1")
        self.assertEqual(d.group, "upstream_sensitive")
        self.assertIn("落后 240", d.reason)
        self.assertIn("真机验证", d.advice)
        self.assertTrue(hits_sensitive(e, RULES))

    def test_commit_detail_fallback_still_detects_sensitive(self):
        # compare 不可用（非 fork）→ 降级到 commit 详情，仍要判出 C 代码
        e = self.by_id["upstream:your-org/app-suite:a7c91fe0002"]
        d = classify(e, CFG, RULES)
        self.assertEqual(d.level, "P1")
        self.assertEqual(d.group, "upstream_sensitive")
        self.assertTrue(any("plugin_a.c" in f for f in hits_sensitive(e, RULES)))

    def test_bot_actor_upstream_not_swallowed_as_p2(self):
        # 关键回归：上游作者是 bot 时，旧逻辑落到 P2「上游例行提交」，
        # 连文件清单不可用都不说，等于静默漏掉。
        e = self.by_id["upstream:your-org/lib-render:5e1b0aa0003"]
        d = classify(e, CFG, RULES)
        self.assertEqual(d.level, "P1")
        self.assertIn("无法判定", d.reason)
        self.assertIn("无法判定", d.advice)

    def test_truncated_compare_not_treated_as_clean(self):
        e = self.by_id["upstream:your-org/app-file-manager:9f0cc1d0004"]
        d = classify(e, CFG, RULES)
        self.assertEqual(d.group, "upstream_sensitive")
        self.assertIn("截断", d.advice)

    def test_identical_upstream_is_quiet(self):
        e = self.by_id["upstream:your-org/app-graphics:1a2b3c40005"]
        d = classify(e, CFG, RULES)
        self.assertEqual(d.level, "P2")
        self.assertIn("无需同步", d.reason)

    def test_multi_upstream_mapping_expands(self):
        # value 为 list 时（一个本地仓库参考多个上游）必须全部展开
        cfg = {"upstream": {"your-org/app-console": ["upstream-org/console-alt",
                                                  "upstream-org/console-app"],
                            "your-org/app-file-manager": "upstream-org/file-manager"}}
        pairs = list(_upstream_pairs(cfg))
        self.assertEqual(len(pairs), 3)
        self.assertIn(("your-org/app-console", "upstream-org/console-app"), pairs)
        self.assertIn(("your-org/app-file-manager", "upstream-org/file-manager"), pairs)

    def test_invalid_upstream_entry_skipped(self):
        cfg = {"upstream": {"r": ["good/one", "not-a-repo", 123]}}
        self.assertEqual(list(_upstream_pairs(cfg)), [("r", "good/one")])


class TestV1Migration(unittest.TestCase):
    """真实 v1 state 的 500 条 notified_ids 必须一条不丢、顺序不变。"""

    def test_save_defends_bare_v1(self):
        v1 = {"last_check": "2026-10-29T09:33:00Z",
              "notified_ids": ["id-%d" % i for i in range(500)],
              "known_repos": {"a": {}}, "repo_baseline": {}, "upstream": {},
              "auto_upstream": {}}
        path = os.path.join(tempfile.gettempdir(), "rm_test_save_v1.json")
        st.save(path, v1)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["version"], 2)
        self.assertNotIn("notified_ids", d)
        self.assertEqual([x["id"] for x in d["deliveries"]],
                         ["id-%d" % i for i in range(500)])
        os.remove(path)

    def test_load_auto_migrates_and_appends_only(self):
        v1 = {"last_check": "2026-10-29T09:33:00Z",
              "notified_ids": ["id-%d" % i for i in range(500)]}
        path = os.path.join(tempfile.gettempdir(), "rm_test_load_v1.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(v1, f)
        s = st.load(path)
        self.assertEqual(s["version"], 2)
        self.assertEqual(len(s["deliveries"]), 500)
        st.mark_notified(s, "new:1", "P0", "新事件")
        ids = [x["id"] for x in s["deliveries"]]
        self.assertEqual(ids[:500], ["id-%d" % i for i in range(500)])
        self.assertEqual(ids[-1], "new:1")
        os.remove(path)


class TestWatermarks(unittest.TestCase):
    """水位键读写必须一一对应：采集端读的键，流水线必须写。"""

    def test_advance_writes_endpoint_keys(self):
        from src.pipeline import _advance_watermarks
        s = st.default_state()
        evs = [
            Event("commit:o/r:a", "push", "o/r", created_at="2026-11-07T07:00:00Z"),
            Event("issue:o/r:1", "issue", "o/r", created_at="2026-11-07T07:01:00Z"),
            Event("comment:o/r:2", "comment", "o/r", created_at="2026-11-07T07:02:00Z"),
            Event("notification:9", "notification", "o/r", created_at="2026-11-07T07:03:00Z"),
        ]
        _advance_watermarks(s, evs)
        wm = s["watermarks"]
        self.assertEqual(wm["commits:o/r"], "2026-11-07T07:00:00Z")
        self.assertEqual(wm["issues:o/r"], "2026-11-07T07:01:00Z")
        self.assertEqual(wm["comments:o/r"], "2026-11-07T07:02:00Z")
        self.assertEqual(wm["notifications"], "2026-11-07T07:03:00Z")
        self.assertEqual(wm["global"], "2026-11-07T07:03:00Z")
        # 细粒度诊断键仍保留，不覆盖既有事实
        self.assertEqual(wm["push:o/r"], "2026-11-07T07:00:00Z")

    def test_since_lags_and_handles_empty(self):
        from src.events import GitHubSource
        src = GitHubSource.__new__(GitHubSource)
        src.state = st.default_state()
        self.assertEqual(GitHubSource._since(src, "commits:o/r"), "")
        src.state["watermarks"]["commits:o/r"] = "2026-11-07T07:00:00Z"
        # 回退 60 秒，避免 since 的严格边界吞掉同一秒的事件
        self.assertEqual(GitHubSource._since(src, "commits:o/r"),
                         "2026-11-07T06:59:00Z")
        src.state["watermarks"]["commits:o/r"] = "not-a-timestamp"
        self.assertEqual(GitHubSource._since(src, "commits:o/r"), "")

    def test_no_mismatch_regression(self):
        """回归保护：不会再退回「只读 commits: 却只写 push:」的错配。"""
        from src.pipeline import _advance_watermarks
        from src.events import GitHubSource
        s = st.default_state()
        _advance_watermarks(s, [Event("commit:o/r:a", "push", "o/r",
                                      created_at="2026-11-07T07:00:00Z")])
        src = GitHubSource.__new__(GitHubSource)
        src.state = s
        self.assertTrue(GitHubSource._since(src, "commits:o/r"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
