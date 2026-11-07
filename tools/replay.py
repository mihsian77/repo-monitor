#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用合成回放数据做离线验收（不需要凭证）。

断言的是「规则在回放数据上的表现」，不是模拟一次把所有历史事件灌进群。
实际运行是增量的，每轮只有新事件；这里同时验证二次运行幂等。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import gh, state as st  # noqa: E402
from src.events import Event, ReplaySource, WebhookSource  # noqa: E402
from src.notify import FeishuClient  # noqa: E402
from src.pipeline import run  # noqa: E402
from src.report import summarize_error  # noqa: E402
from src.rules import classify  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    with open(os.path.join(ROOT, "config/settings.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    with open(os.path.join(ROOT, "config/rules.json"), encoding="utf-8") as f:
        rules = json.load(f)
    fx = os.path.join(ROOT, "tests/fixtures/events-sample.json")
    src = ReplaySource(fx)
    now = src.captured_at

    print("=== A. 合成数据回放 ===")
    s = st.default_state()
    s["heartbeat"]["runs"] = 1
    s["heartbeat"]["last_success"] = now
    r = run(src, s, cfg, rules, FeishuClient(dry_run=True),
            now=now, bootstrap=False, save_path=None)
    lv = r["by_level"]
    cards = r["cards"]
    by_title = [c["title"] for c in cards]

    check("采集到 >=600 条合成事件", r["events"] >= 600, f"{r['events']} 条")
    check("两条外部真人 Issue 被判 P0",
          any("下载失败" in " ".join(c["lines"]) for c in cards if c["level"] == "P0") and
          any("示例：编译请求" in " ".join(c["lines"]) for c in cards if c["level"] == "P0"),
          "app-primary 下载失败 + app-backup 编译请求")
    check("50 条 CodeRabbit 模板评论全部静默", lv.get("DROP", 0) == 50, f"DROP={lv.get('DROP')}")
    check("33 个 Release 聚合为 1 张 P1 卡",
          sum(1 for c in cards if c["group"] == "release") == 1 and
          any("33 条" in t for t in by_title), "同批多版本不逐条推")
    check("8 次自身失败合并为 1 张心跳卡",
          sum(1 for c in cards if c["group"] == "heartbeat") == 1, "不淹没真实告警")
    check("17 次真实构建失败按仓库合并，卡片 <=8",
          0 < sum(1 for c in cards if c["group"] == "build") <= 8,
          f"{sum(1 for c in cards if c['group']=='build')} 张")
    check("总卡片 <=20（告警疲劳控制）", r["card_count"] <= 20, f"{r['card_count']} 张")
    check("本人回复触发待办销账 2 件", r["todos_closed"] == 2,
          "app-backup / app-primary 本人已回复，应自动销账")
    check("待办本指出最老 38 天", r["todos"]["oldest_days"] >= 30,
          f"最老 {r['todos']['oldest_days']} 天")

    print("=== B. 二次运行幂等（相同状态 + 相同时间）===")
    r2 = run(src, s, cfg, rules, FeishuClient(dry_run=True),
             now=now, bootstrap=False, save_path=None)
    check("无重复推送", r2["delivery"]["sent"] == 0,
          f"sent={r2['delivery']['sent']}")

    print("=== C. 错误总结 / 只读守卫 / Webhook / 状态迁移 ===")
    fake_log = "\n".join([
        "2026-11-05T10:00:00Z ##[section]Starting: Build",
        "2026-11-05T10:00:01Z 0 errors, 0 warnings",
        "2026-11-05T10:00:09Z ld: error: undefined reference to 'sample_module_init'",
        "2026-11-05T10:00:10Z collect2: error: ld returned 1 exit status",
        "2026-11-05T10:00:11Z ##[error]Process completed with exit code 1.",
    ])
    err = summarize_error(fake_log, rules)
    check("错误归类为链接错误且过滤 '0 errors'",
          err["category"] == "link" and "0 errors" not in err["snippet"],
          f"category={err['category']}, snippet={err['snippet'][:60]}")

    try:
        gh.assert_readonly("POST")
        blocked = False
    except gh.ReadOnlyViolation:
        blocked = True
    check("运行时守卫拦截非 GET", blocked)

    wh = WebhookSource("push", {
        "repository": {"full_name": "your-org/app-primary"},
        "sender": {"login": "your-org"}, "pusher": {"name": "your-org"},
        "ref": "refs/heads/main", "after": "abcdef123456",
        "head_commit": {"timestamp": "2026-11-05T10:00:00Z"},
        "commits": [{"id": "x", "message": "fix: 下载引擎握手重试",
                     "added": [], "modified": ["src/main/cpp/download/engine.c"],
                     "removed": []}],
        "compare": "https://github.com/your-org/app-primary/compare/a...b",
    }, delivery_id="del-001")
    evs = wh.collect(s, cfg)
    d = classify(evs[0], cfg, rules)
    check("Webhook push 带文件清单且命中敏感路径判 P1",
          "src/main/cpp/download/engine.c" in evs[0].meta.get("files", []) and d.level == "P1",
          f"level={d.level}, advice={d.advice[:50]}")

    v1 = {"last_check": "2026-10-29T09:33:00Z",
          "notified_ids": ["id-a", "id-b", "id-c"],
          "known_repos": {"your-org/app-primary": {"fork": False}},
          "upstream": {"your-org/app-file-manager": "abc1234"},
          "repo_baseline": {}, "auto_upstream": {}}
    mig = st.migrate_v1(v1)
    check("state v1→v2 保留全部历史去重 ID 与基线",
          len(mig["deliveries"]) == 3 and
          mig["known_repos"] == v1["known_repos"] and
          mig["upstream"] == v1["upstream"],
          "只追加，未覆盖、未精简")

    print("=== D. 明文密钥扫描 ===")
    import subprocess
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools/check_readonly.py")],
                       capture_output=True, text=True)
    guard = json.JSONDecoder().raw_decode(p.stdout)[0]
    check("静态只读守卫通过", guard["passed"],
          f"扫描 {guard['checked_files']} 个文件，违规 {len(guard['violations'])} 处")

    print("=== E. 上游链路回放（专用 fixture，不污染 616 条主数据）===")
    from src.events import _upstream_pairs, parse_upstream_compare
    from src.rules import files_status
    up_fx = os.path.join(ROOT, "tests/fixtures/upstream-sample.json")
    up_src = ReplaySource(up_fx)
    check("上游专用 fixture 含 5 条事件", len(up_src.events) == 5,
          f"{len(up_src.events)} 条")
    us = st.default_state()
    us["heartbeat"]["runs"] = 1
    us["heartbeat"]["last_success"] = now
    ur = run(up_src, us, cfg, rules, FeishuClient(dry_run=True),
             now=now, bootstrap=False, save_path=None)
    up_levels = ur["by_level"]
    check("上游事件不再落到 P2 例行提交",
          up_levels.get("P1", 0) >= 4 and up_levels.get("P2", 0) == 1,
          f"P1={up_levels.get('P1')}, P2={up_levels.get('P2')}")
    up_cards = ur["cards"]
    sens = [c for c in up_cards if c["group"] == "upstream_sensitive"]
    check("命中敏感路径的上游单独成卡", len(sens) == 1,
          f"{len(sens)} 张")
    sens_text = " ".join(" ".join(c["lines"]) for c in sens)
    check("上游卡片给出落后量与真机验证建议",
          "落后 240" in sens_text and "真机验证" in sens_text)
    check("compare 截断被明说，未当成『没命中』",
          any("截断" in ln for c in up_cards for ln in c["lines"]))
    check("文件清单不可用被明说，未静默判安全",
          any("无法判定" in ln for c in up_cards for ln in c["lines"]))
    check("上游同步待办已建账", ur["new_todos"] >= 4,
          f"新增 {ur['new_todos']} 件")
    ur2 = run(up_src, us, cfg, rules, FeishuClient(dry_run=True),
              now=now, bootstrap=False, save_path=None)
    check("上游二次运行 0 重复推送", ur2["delivery"]["sent"] == 0,
          f"sent={ur2['delivery']['sent']}")
    check("compare 截断检测（250 commits / 310 total）",
          parse_upstream_compare({"ahead_by": 0, "behind_by": 310,
                                  "total_commits": 310, "status": "behind",
                                  "commits": [{"sha": "x"}] * 250,
                                  "files": [{"filename": "a.c"}]})["files_truncated"])
    check("多来源上游映射可展开（app-console 两个参考源）",
          len(list(_upstream_pairs({"upstream": {
              "your-org/app-console": ["upstream-org/console-alt", "upstream-org/console-app"]}}))) == 2)
    check("无 files 的老上游事件不会被误判为『已判定安全』",
          files_status(Event("old", "upstream", "r", meta={})) == "unavailable")

    print("=== F. 分类合并转发（QQ 合并转发效果，本次新增）===")
    from src.digest import build_digest
    from src.notify import build_card_payload
    from src.report import Card

    fs = st.default_state()
    fs["heartbeat"]["runs"] = 1
    fs["heartbeat"]["last_success"] = now
    fr = run(src, fs, cfg, rules, FeishuClient(dry_run=True),
             now=now, bootstrap=False, save_path=None)
    dl = fr["delivery"]
    check("一轮从 14 条平铺压到 2 条消息",
          0 < dl["sent"] <= 2 and dl["merged"] >= 10,
          f"sent={dl['sent']} merged={dl['merged']}（1 汇总卡 + 1 在岗卡）")
    check("按类别分折叠面板且 P0 默认展开",
          dl["groups"] >= 5 and dl["shards"] == 1,
          f"groups={dl['groups']} shards={dl['shards']}")

    # 在岗确认由 deliver 单独发，不进汇总卡；这里只取真正被合并的成员
    fcards = [Card(c["id"], c["level"], c["title"], c["lines"],
                   c["event_ids"], c["group"]) for c in fr["cards"]
              if c["group"] != "daily_checkin"]
    fd = build_digest(fcards, cfg, now)
    panels = [e for e in fd.payload["card"]["body"]["elements"]
              if e.get("tag") == "collapsible_panel"]
    check("卡片是 interactive + JSON 2.0 + 折叠面板",
          fd.payload["msg_type"] == "interactive" and
          fd.payload["card"]["schema"] == "2.0" and len(panels) == dl["groups"])
    check("P0 面板展开、P1/P2 面板折叠",
          all(p["expanded"] for p in panels
              if "🔴" in p["header"]["title"]["content"]) and
          any(not p["expanded"] for p in panels
              if "🟠" in p["header"]["title"]["content"] or
                 "🟡" in p["header"]["title"]["content"]))
    check("同类卡片归到同一面板（构建失败 6 项一组）",
          any("构建失败" in p["header"]["title"]["content"] and
              "6 项" in p["header"]["title"]["content"] for p in panels))
    signed = build_card_payload(fd.payload["card"], "sec", "1700000000")
    check("含签名仍低于飞书 20KB 上限",
          len(json.dumps(signed, ensure_ascii=False).encode("utf-8")) < 20 * 1024,
          f"digest_bytes={dl['digest_bytes']}")
    check("二次运行 0 重复推送（合并记账幂等）",
          run(src, fs, cfg, rules, FeishuClient(dry_run=True),
              now=now, bootstrap=False, save_path=None)["delivery"]["sent"] == 0)

    # 超大内容：自动分片，且成员一个不少
    huge = [Card(f"h{i}", "P2", f"【P2】类别{i}",
                 [("长日志内容 " * 40) for _ in range(30)],
                 [f"he{i}"], f"hg{i}") for i in range(15)]
    hd = build_digest(huge, dict(cfg, grouping=dict(cfg["grouping"],
                                                    max_payload_bytes=6000)), now)
    check("超预算自动分片，绝不丢成员",
          hd.shards > 1 and hd.size_bytes <= 6000 and
          len(hd.member_ids) == len(huge) and hd.truncated,
          f"shards={hd.shards} max_bytes={hd.size_bytes}")

    # 开关一键回退
    off_cfg = dict(cfg, grouping=dict(cfg["grouping"], enabled=False))
    offs = st.default_state()
    offs["heartbeat"]["runs"] = 1
    offs["heartbeat"]["last_success"] = now
    off_r = run(src, offs, off_cfg, rules, FeishuClient(dry_run=True),
                now=now, bootstrap=False, save_path=None)
    check("grouping.enabled=false 一键退回逐条推送",
          off_r["delivery"]["merged"] == 0 and
          off_r["delivery"]["sent"] == off_r["card_count"],
          f"sent={off_r['delivery']['sent']}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== 验收结果：{passed}/{total} 通过 ===")
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  FAIL: {name} — {detail}")
        return 1
    print("全部通过。这套规则在合成回放数据上不会刷屏，也不会漏掉真人信号。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
