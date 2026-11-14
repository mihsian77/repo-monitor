#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隐私投影层验收：公开 state.json 里绝不能出现真实仓库标识。

用合成的「真实名」（acme-corp/...）做测试，测试文件本身不含任何真实信息。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from src import privacy, state as st  # noqa: E402
from src.config_loader import is_template, load_settings  # noqa: E402

# 合成的真实清单（看起来像真的，但不指向任何真实账号/仓库）
REAL_CFG = {
    "owner": "acme-corp",
    "self_repo": "acme-corp/monitor-hub",
    "repos": ["acme-corp/app-core", "acme-corp/app-ui", "acme-corp/lib-base"],
    "default_branches": {"acme-corp/app-core": "main", "acme-corp/lib-base": "dev"},
    "upstream": {"acme-corp/lib-base": "vendor-org/base-lib"},
    "upstream_candidates": {"acme-corp/app-ui": ["vendor-org/ui-kit"]},
    "bots": ["acme-corp-bot[bot]", "github-actions[bot]"],
    "extra_repos": [],
}


class PrivacyProjectionTest(unittest.TestCase):
    def tearDown(self):
        privacy.reset()
        os.environ.pop("MONITOR_CONFIG", None)

    def test_template_mode_disables_projection(self):
        """别人 clone 模板（无 Secret）时必须是 no-op，行为与旧版一致。"""
        cfg = load_settings(ROOT)
        self.assertTrue(is_template(cfg))
        self.assertFalse(privacy.enabled())
        self.assertEqual(privacy.scrub_text("some/repo"), "some/repo")

    def test_injected_config_enables_projection(self):
        os.environ["MONITOR_CONFIG"] = json.dumps(REAL_CFG)
        cfg = load_settings(ROOT)
        self.assertFalse(is_template(cfg))
        self.assertTrue(privacy.enabled())
        self.assertEqual(cfg["owner"], "acme-corp")
        self.assertEqual(len(cfg["repos"]), 3)
        # 行为参数仍以仓库模板为准，不被 Secret 夹带覆盖
        self.assertTrue(cfg["grouping"]["enabled"])

    def test_scrub_unscrub_roundtrip(self):
        privacy.configure(REAL_CFG, enabled=True)
        samples = [
            "push:acme-corp/app-core",
            "构建失败 · acme-corp/lib-base · undefined reference",
            "https://github.com/acme-corp/app-ui/issues/12",
            "上游 vendor-org/base-lib 领先 12 个提交",
        ]
        for s in samples:
            scrubbed = privacy.scrub_text(s)
            self.assertNotIn("acme-corp", scrubbed)
            self.assertNotIn("vendor-org", scrubbed)
            self.assertEqual(privacy.unscrub_text(scrubbed), s)

    def test_state_file_never_contains_real_names(self):
        """核心验收：state.save() 写出的文件（会被 commit 回公开仓库）零明文。"""
        privacy.configure(REAL_CFG, enabled=True)
        s = st.default_state()
        s["watermarks"]["push:acme-corp/app-core"] = "2026-01-01T00:00:00Z"
        s["known_repos"]["acme-corp/lib-base"] = {"fork": True}
        s["deliveries"].append({"id": "e:acme-corp/app-ui:1", "ts": "2026-01-01T00:00:00Z",
                                "level": "P0", "title": "构建失败 · acme-corp/app-ui"})
        s["todos"].append({"repo": "acme-corp/app-core", "title": "修复链接错误"})
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            st.save(p, s)
            raw = open(p, encoding="utf-8").read()
        for secret in ("acme-corp", "app-core", "app-ui", "lib-base",
                       "vendor-org", "base-lib", "monitor-hub"):
            self.assertNotIn(secret, raw, f"公开 state.json 泄露了 {secret}")
        self.assertIn("_privacy", raw)
        # 内存中的原对象不被投影污染
        self.assertTrue(any("acme-corp/app-core" in k for k in s["watermarks"]))

    def test_load_restores_names_and_dedup_still_works(self):
        """读盘还原后，去重/水位逻辑完全不受影响。"""
        privacy.configure(REAL_CFG, enabled=True)
        s = st.default_state()
        eid = "push:acme-corp/app-core:abc123"
        st.mark_notified(s, eid, "P0", "关键提交")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            st.save(p, s)
            loaded = st.load(p)
        self.assertTrue(st.is_notified(loaded, eid))
        self.assertNotIn("_privacy", loaded)
        self.assertTrue(privacy.enabled())

    def test_different_salt_produces_different_tokens(self):
        privacy.configure(REAL_CFG, salt="salt-A", enabled=True)
        a = privacy.scrub_text("acme-corp/app-core")
        privacy.configure(REAL_CFG, salt="salt-B", enabled=True)
        b = privacy.scrub_text("acme-corp/app-core")
        self.assertNotEqual(a, b)
        # 换 salt 后旧 token 无法还原（等价于重置去重账，文档里已警示）
        self.assertEqual(privacy.unscrub_text(a), a)

    def test_placeholder_filter_does_not_swallow_real_names(self):
        """真实名里含 demo/example 等词也不能被当成占位符漏掉。"""
        cfg = dict(REAL_CFG)
        cfg["repos"] = cfg["repos"] + ["acme-corp/example-tools", "acme-corp/demo-app"]
        privacy.configure(cfg, enabled=True)
        names = privacy.collect_identifiers(cfg)
        self.assertIn("acme-corp/example-tools", names)
        self.assertIn("acme-corp/demo-app", names)
        self.assertNotIn("acme-corp/example-tools",
                         privacy.scrub_text("acme-corp/example-tools"))

    def test_branch_reference_value_is_collected(self):
        """回归：default_branches 的「值」可能是上游分支引用，
        历史上只收键名，导致 commit message 里的分支名明文写进公开 state。"""
        cfg = dict(REAL_CFG)
        cfg["default_branches"] = {"acme-corp/lib-base": "vendor-x/4.1"}
        names = privacy.collect_identifiers(cfg)
        self.assertIn("vendor-x/4.1", names)
        self.assertIn("vendor-x", names)
        privacy.configure(cfg, enabled=True)
        self.assertNotIn("vendor-x", privacy.scrub_text(
            "Merge remote-tracking branch 'upstream/vendor-x/4.1' into sync-ups"))

    def test_free_text_residual_never_leaks_and_stays_reversible(self):
        """回归：commit message / issue 标题里冒出白名单没列全的片段时，
        写盘必须零明文；且展示字段之外的主键、台账逐字不变。"""
        cfg = dict(REAL_CFG)
        cfg["default_branches"] = {"acme-corp/lib-base": "vendor-x/4.1"}
        privacy.configure(cfg, salt="test-salt", enabled=True)
        s = st.default_state()
        eid = "commit:acme-corp/lib-base:deadbeef"
        st.mark_notified(s, eid, "P1",
                         "Merge branch 'upstream/vendor-x/4.1' into acme-corp/lib-base")
        s["watermarks"]["commits:acme-corp/lib-base"] = "2026-01-01T00:00:00Z"
        s["lessons"].append({"wrong": "漏收 default_branches 的值",
                             "right": "键和值都要进标识集", "date": "2026-01-01"})
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            st.save(p, s)
            raw = open(p, encoding="utf-8").read()
            loaded = st.load(p)
        for secret in ("vendor-x", "acme-corp", "lib-base", "vendor-org"):
            self.assertNotIn(secret, raw, f"公开 state.json 泄露了 {secret}")
        # 主键与台账逐字还原，不能被兜底改写
        self.assertTrue(st.is_delivered(loaded, eid))
        self.assertIn("commits:acme-corp/lib-base", loaded["watermarks"])
        self.assertEqual(loaded["lessons"][0]["wrong"], "漏收 default_branches 的值")
        # deliveries[].title 是纯展示备注，写盘整段脱敏；读盘保持幂等 token，
        # 不再出现真实标识，也不影响任何去重逻辑。
        self.assertIsInstance(loaded["deliveries"][0]["title"], str)
        self.assertNotIn("vendor-x", loaded["deliveries"][0]["title"])
        self.assertNotIn("acme-corp", loaded["deliveries"][0]["title"])

    def test_find_leaks_is_case_insensitive_and_ignores_generic_words(self):
        cfg = dict(REAL_CFG)
        privacy.configure(cfg, enabled=True)
        self.assertTrue(privacy.find_leaks("VENDOR-ORG/base-lib 有更新"))
        self.assertEqual(privacy.find_leaks("main master github-actions[bot]"), [])

    def test_deliveries_title_is_fully_redacted_and_idempotent(self):
        """deliveries[].title 整段脱敏：任意外部自由文本都不写进公开仓库，
        且写盘 → 读盘 → 再写盘 幂等，不会每轮 force push 出新内容。"""
        cfg = dict(REAL_CFG)
        privacy.configure(cfg, salt="test-salt", enabled=True)
        s = st.default_state()
        st.mark_notified(s, "commit:acme-corp/app-core:abc", "P1",
                         "Merge internal-secret-codename into acme-corp/app-core")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            st.save(p, s)
            first = open(p, encoding="utf-8").read()
            loaded = st.load(p)
            st.save(p, loaded)
            second = open(p, encoding="utf-8").read()
        self.assertNotIn("internal-secret-codename", first)
        self.assertEqual(json.loads(first)["deliveries"][0]["title"],
                         json.loads(second)["deliveries"][0]["title"])
        self.assertTrue(st.is_delivered(loaded, "commit:acme-corp/app-core:abc"))

    def test_generic_words_do_not_get_filtered_out_of_real_repo_names(self):
        """通用技术词可能正好是真实仓库名的一部分，不能被误判成占位符。"""
        cfg = dict(REAL_CFG)
        cfg["repos"] = ["acme-corp/driver-manager", "acme-corp/components-builder"]
        names = privacy.collect_identifiers(cfg)
        self.assertIn("acme-corp/driver-manager", names)
        self.assertIn("acme-corp/components-builder", names)

    def test_case_variants_restore_exact_original_case(self):
        """每种大小写变体用独立 token，unscrub 后必须逐字还原，不能串味。

        配置里往往只写一种写法，外部事件 id 里却可能出现小写、全大写或
        驼峰混合。若所有变体共用一个 token，还原后大小写会漂移，去重账
        就被破坏。样例一律用中性占位名，不引用任何真实项目。
        """
        cfg = {
            "owner": "acme-corp",
            "self_repo": "acme-corp/repo-monitor",
            "repos": ["acme-corp/Widgetbox", "acme-corp/SampleHub",
                      "acme-corp/Parts.-Tzst-Viewer"],
            "default_branches": {}, "upstream": {}, "bots": [],
        }
        privacy.configure(cfg, salt="case-salt", enabled=True)
        for word in ("Widgetbox", "widgetbox", "WIDGETBOX", "WidgetBox",
                     "viewer", "VIEWER"):
            original = "prefix:" + word + ":suffix"
            cleaned = privacy.scrub_text(original)
            self.assertNotIn(word, cleaned)
            self.assertEqual(privacy.unscrub_text(cleaned), original)

    def test_case_variants_in_primary_keys_are_scrubbed_and_reversible(self):
        """deliveries[].id 里的小写项目简称必须被可逆投影，且去重仍然命中。"""
        cfg = {
            "owner": "acme-corp",
            "self_repo": "acme-corp/repo-monitor",
            "repos": ["acme-corp/Widgetbox", "acme-corp/SampleHub",
                      "acme-corp/Parts.-Tzst-Viewer"],
            "default_branches": {}, "upstream": {}, "bots": [],
        }
        privacy.configure(cfg, salt="case-salt", enabled=True)
        s = st.default_state()
        st.mark_notified(s, "commit:acme-corp/widgetbox:deadbeef", "P2", "routine")
        st.mark_notified(s, "commit:acme-corp/viewer:cafebabe", "P2", "routine")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            st.save(p, s)
            raw = open(p, encoding="utf-8").read()
            loaded = st.load(p)
        self.assertEqual(privacy.find_leaks(raw), [])
        self.assertNotIn("widgetbox", raw)
        self.assertNotIn("viewer", raw)
        self.assertTrue(st.is_delivered(loaded, "commit:acme-corp/widgetbox:deadbeef"))
        self.assertTrue(st.is_delivered(loaded, "commit:acme-corp/viewer:cafebabe"))


if __name__ == "__main__":
    unittest.main()
