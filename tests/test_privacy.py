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


if __name__ == "__main__":
    unittest.main()
