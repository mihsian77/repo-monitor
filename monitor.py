#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repo-monitor 入口。

用法：
  python3 monitor.py                     # 正常运行（只读采集 + 飞书推送 + 回写 state.json）
  python3 monitor.py --dry-run           # 真实采集但不推送、不写状态
  python3 monitor.py --replay            # 用内置合成数据回放（离线，验证分级逻辑）
  python3 monitor.py --self-test         # 自检：配置、只读守卫、模块导入
  python3 monitor.py --demo              # 用合成数据真发一张「分类合并卡」，验证视觉效果
  python3 monitor.py --check-heartbeat   # 只检查心跳并告警

真实仓库清单不放在仓库里，由 MONITOR_CONFIG（JSON）或
config/settings.local.json 注入；state.json 写盘前自动做隐私投影。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from src import gh, privacy, state as st  # noqa: E402
from src.config_loader import is_template, load_settings  # noqa: E402
from src.events import GitHubSource, ReplaySource  # noqa: E402
from src.notify import (FeishuClient, send_heartbeat_alert,  # noqa: E402
                        should_skip_run)
from src.pipeline import run  # noqa: E402


def _load_dotenv() -> None:
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def load_config() -> tuple[dict, dict]:
    cfg = load_settings(ROOT)
    with open(os.path.join(ROOT, "config/rules.json"), encoding="utf-8") as f:
        rules = json.load(f)
    return cfg, rules


def _safe(s) -> str:
    """公开日志（Actions log 任何人可读）必须脱敏后再打印。"""
    return privacy.scrub_text(str(s))


def print_summary(result: dict) -> None:
    """人可读摘要。完整 JSON 用 --full（同样脱敏）。"""
    print("=== repo-monitor 运行摘要 ===")
    for k in ("bootstrap", "events", "fresh", "duplicates", "by_level",
              "card_count", "new_todos", "todos_closed", "delivery", "dead_letters"):
        if k in result:
            print(f"  {k}: {_safe(json.dumps(result[k], ensure_ascii=False))}")
    if result.get("todos"):
        t = result["todos"]
        print(f"  待办本: 还欠 {t['open']} 件，最老 {t['oldest_days']} 天，超期 {t['escalated']} 件")
    if result.get("note"):
        print("  说明:", _safe(result["note"]))
    cards = result.get("cards", [])
    if cards:
        print(f"=== 本轮将推送 {len(cards)} 张卡片 ===")
        for c in cards:
            print(f"  [{c['level']}] {_safe(c['title'])}  ({len(c['lines'])} 行)")
    p0 = [c for c in cards if c["level"] == "P0"]
    if p0:
        print("=== P0 明细（前 2 张）===")
        for c in p0[:2]:
            for ln in c["lines"]:
                print("   ", _safe(ln))


def self_test(cfg: dict, rules: dict) -> int:
    print("=== repo-monitor 自检 ===")
    ok = True
    try:
        gh.assert_readonly("GET")
        print("  [OK] 只读守卫放行 GET")
    except Exception as e:
        print("  [FAIL]", e)
        ok = False
    try:
        gh.assert_readonly("POST")
        print("  [FAIL] 非 GET 未被拦截")
        ok = False
    except gh.ReadOnlyViolation:
        print("  [OK] 只读守卫拦截 POST（ReadOnlyViolation）")
    mode = "模板占位（未注入真实清单）" if is_template(cfg) else "已注入真实清单"
    print(f"  [OK] 配置模式：{mode} / {len(cfg['repos'])} 个仓库 / "
          f"{len(cfg['upstream'])} 个上游映射")
    print(f"  [OK] 隐私投影：{'启用（state.json 写盘脱敏）' if privacy.enabled() else '关闭（模板状态）'}")
    print(f"  [OK] 规则：{len(rules.get('error_categories', []))} 类错误分类 / "
          f"{len(rules.get('sensitive_paths', []))} 条敏感路径规则")
    print(f"  [OK] 飞书地址来源：{'环境变量' if os.environ.get('FEISHU_WEBHOOK') else '未配置（dry-run）'}")
    print(f"  [OK] 飞书签名校验：{'已启用（FEISHU_SECRET）' if os.environ.get('FEISHU_SECRET') else '未启用（留空亦可正常运行）'}")
    print("  [OK] 模块导入：gh / state / privacy / config_loader / events / rules / report / notify / pipeline")
    return 0 if ok else 1


def demo(cfg: dict, client: FeishuClient) -> int:
    """用纯合成数据真发一张分类合并卡，验证「QQ 合并转发」视觉效果。

    - 不采集任何真实仓库、不读也不写 state.json；
    - 所有文案用 demo-* 占位名，公开日志与飞书卡片都不含真实信息；
    - 走的是与生产完全相同的 build_digest + send_card 链路。
    """
    from src.digest import build_digest
    from src.report import Card

    samples = [
        Card("demo:build:a", "P0", "【P0】构建失败 · demo-app-core",
             ["- 工作流：CI / build", "- undefined reference to `init_engine`",
              "- 建议：检查链接顺序与静态库依赖"], ["demo:build:a"], "build"),
        Card("demo:build:b", "P0", "【P0】构建失败 · demo-app-ui",
             ["- 工作流：CI / build", "- error: cannot find symbol `R.layout.main`",
              "- 建议：确认资源文件已提交"], ["demo:build:b"], "build"),
        Card("demo:issue", "P0", "【P0】真人 Issue · demo-app-core",
             ["- 外部用户反馈启动失败", "- 建议：优先复现并回复"], ["demo:issue"], "issue"),
        Card("demo:release", "P1", "【P1】Release 批量发布（3 条）",
             ["- demo-lib-base v1.2.0", "- demo-lib-base v1.2.1", "- demo-app-core v2.0.0-rc1"],
             ["demo:release"], "release"),
        Card("demo:pr", "P1", "【P1】PR / 上游同步（2 条）",
             ["- demo-app-core #42 已合并", "- demo-app-ui #17 等待评审"],
             ["demo:pr"], "pr"),
        Card("demo:upstream", "P1", "【P1】上游有更新 · demo-lib-base",
             ["- 领先 12 个提交", "- 涉及 3 个敏感路径文件"],
             ["demo:upstream"], "upstream"),
        Card("demo:todo", "P1", "【P1】待办已超期",
             ["- 2 件待办超过 7 天未销账"], ["demo:todo"], "silence"),
        Card("demo:daily", "P2", "【P2】例行日报",
             ["- 今日 128 条事件", "- 关键提交 2 条 / 评论 41 条", "- 其余归入例行日报"],
             ["demo:daily"], "daily"),
    ]
    now = st.now_utc()
    digest = build_digest(samples, cfg, now)
    print(f"=== demo 合并卡（合成数据，不含任何真实仓库）===")
    print(f"  逻辑卡片 {len(samples)} 张 → 分片 {digest.shards} 条 / "
          f"{digest.groups} 个分类面板 / 最大单片 {digest.size_bytes} 字节")
    for i, payload in enumerate(digest.payloads, 1):
        els = payload["card"]["body"]["elements"]
        panels = [e for e in els if e.get("tag") == "collapsible_panel"]
        print(f"  第 {i} 片：{len(panels)} 个面板（"
              f"{', '.join(p['header']['title']['content'] for p in panels)}）")

    ok = True
    for payload in digest.payloads:
        if not client.send_card(payload["card"]):
            ok = False
    if client.dry_run:
        print("  [dry-run] 未实际推送；在 Actions 里配置了 Webhook 即会真发到群里。")
    else:
        print("  推送结果：", "全部送达" if ok else "有分片失败（已保留降级能力）")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="repo-monitor（单仓库只读管家 · 可公开模板）")
    ap.add_argument("--dry-run", action="store_true", help="不推送、不写状态")
    ap.add_argument("--replay", action="store_true", help="离线回放合成数据")
    ap.add_argument("--replay-file", default="", help="自定义回放 fixture 路径")
    ap.add_argument("--self-test", action="store_true", help="自检")
    ap.add_argument("--demo", action="store_true", help="用合成数据发一张分类合并卡")
    ap.add_argument("--check-heartbeat", action="store_true", help="只检查心跳")
    ap.add_argument("--state", default=os.path.join(ROOT, "state.json"))
    ap.add_argument("--verbose", action="store_true", help="dry-run 时逐条打印将要推送的消息")
    ap.add_argument("--full", action="store_true", help="输出完整 JSON 结果而非摘要")
    args = ap.parse_args()

    _load_dotenv()
    cfg, rules = load_config()

    if args.self_test:
        return self_test(cfg, rules)

    dry = args.dry_run or os.environ.get("DRY_RUN", "") == "1"

    # demo 独立通道：不加载 state、不采集、不写盘
    if args.demo:
        return demo(cfg, FeishuClient(dry_run=dry, verbose=args.verbose))

    state = st.load(args.state)
    client = FeishuClient(dry_run=dry, verbose=args.verbose)

    # 代码已上线但 Secrets 还没填的空窗期：整轮空转，绝不碰状态与水位。
    if should_skip_run(client, dry, replay=args.replay):
        print("[skip] 未配置 FEISHU_WEBHOOK：本轮空转（不采集、不记账、不写状态）。")
        print("       在仓库 Secrets 填好 FEISHU_WEBHOOK 后，下一轮自动恢复。")
        return 0

    if args.check_heartbeat:
        from src.report import heartbeat_card
        c = heartbeat_card(state, cfg, st.now_utc())
        if c:
            print("心跳超时，发送告警")
            return 0 if send_heartbeat_alert(client, c.lines) else 1
        print("心跳正常：", state["heartbeat"].get("last_success", ""))
        return 0

    if args.replay:
        path = args.replay_file or os.path.join(ROOT, "tests/fixtures/events-sample.json")
        src = ReplaySource(path)
        replay_state = st.default_state()
        replay_state["heartbeat"]["runs"] = 1
        replay_state["heartbeat"]["last_success"] = src.captured_at
        result = run(src, replay_state, cfg, rules,
                     FeishuClient(dry_run=True, verbose=args.verbose),
                     now=src.captured_at, bootstrap=False, save_path=None)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.full
              else print_summary(result))
        return 0

    token = os.environ.get("GH_READ_TOKEN", "")
    if not token:
        print("[warn] 未配置 GH_READ_TOKEN，仅能访问公开数据（限额 60/h）")
    client_gh = gh.GitHubClient(token=token, etag_store=state.get("etags"))
    src = GitHubSource(client_gh, cfg, state)
    save_path = None if dry else args.state
    result = run(src, state, cfg, rules, client, save_path=save_path,
                 fetch_logs=not dry)
    state["etags"] = client_gh.etags
    if args.full:
        print(_safe(json.dumps(result, ensure_ascii=False, indent=2)))
    else:
        print_summary(result)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except gh.ReadOnlyViolation as e:
        print(f"[FATAL] 只读守卫触发，本轮中止：{e}", file=sys.stderr)
        sys.exit(2)
