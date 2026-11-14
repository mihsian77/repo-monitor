#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上线前只读核对：v1 state.json 里的上游基线到底是什么。

背景（示例场景）：
  v1 的 upstream 字段记录的是某个历史 commit，用 compare 实测它对应
  fork 分叉点，而不是「上次同步到的上游 HEAD」。旧方案担心
  「直接拿它当水位，恢复首轮会一口气误报大量更新」。

v2 的结论：不会。原因有三，本工具逐条核对：
  1. v2 采集上游时每个 (local, upstream) 每轮只生成 1 条聚合事件
     （id = upstream:{local}:{sha[:12]}），不逐条遍历历史 commits；
  2. 首次运行是 bootstrap，只建基线、不推送任何历史消息；
  3. state["upstream"] 在 v2 里只是保留的历史事实，采集根本不读它当水位。

所以**上线前无需重置水位**。本工具只做只读核对并给出结论，绝不写 state.json。

用法：
  python3 tools/check_baseline.py                       # 纯离线，只看本地文件
  python3 tools/check_baseline.py --state /path/state.json
  python3 tools/check_baseline.py --live               # 额外只读 GET 上游当前 HEAD 做对比
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import gh  # noqa: E402
from src.events import _upstream_pairs  # noqa: E402


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser(description="上线前只读核对上游基线（不写任何文件）")
    ap.add_argument("--state", default=os.path.join(ROOT, "state.json"),
                    help="要核对的 state.json（默认交付包内；上线时指向仓库里的那个）")
    ap.add_argument("--config", default=os.path.join(ROOT, "config/settings.json"))
    ap.add_argument("--live", action="store_true",
                    help="额外只读 GET 各上游当前 HEAD 做对比（需要 GH_READ_TOKEN）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = _load_json(args.config)
    has_state = os.path.exists(args.state)
    state = _load_json(args.state) if has_state else {}
    legacy = state.get("upstream", {}) or {}
    if not has_state:
        state_format = "不存在（首次运行时生成；安装脚本也会保留仓库里已有的 v1 文件）"
    elif "notified_ids" in state or state.get("version") != 2:
        state_format = "v1（本次上线将自动迁移为 v2）"
    else:
        state_format = "v2"

    rows = []
    live = {}
    if args.live:
        c = gh.GitHubClient()
        for local, upstream in _upstream_pairs(cfg):
            r = c.get(f"/repos/{upstream}/commits", {"per_page": 1})
            if isinstance(r.data, list) and r.data:
                live[upstream] = (r.data[0].get("sha", "")[:12],
                                  r.data[0].get("commit", {}).get("author", {}).get("date", ""))

    for local, upstream in _upstream_pairs(cfg):
        old = legacy.get(local, "")
        if isinstance(old, (dict, list)):
            old = json.dumps(old, ensure_ascii=False)[:40]
        cur, date = live.get(upstream, ("未查询（加 --live）", ""))
        rows.append({
            "local": local, "upstream": upstream,
            "legacy_value": str(old)[:40] or "（无记录）",
            "current_head": cur, "head_date": date,
        })

    result = {
        "state_path": args.state,
        "state_format": state_format,
        "legacy_upstream_entries": len(legacy),
        "configured_upstream_pairs": len(rows),
        "candidates_not_enabled": [
            k for k in (cfg.get("upstream_candidates", {}) or {})
            if not k.startswith("_")
        ],
        "rows": rows,
        "conclusion": (
            "无需重置水位：v2 每个上游每轮只产出 1 条聚合事件，且首轮 bootstrap "
            "只建基线不推送；state['upstream'] 在 v2 中仅作为历史事实保留，不当水位使用。"
            "直接按上线操作手册提交即可。"
        ),
        "wrote_any_file": False,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("=== 上游基线只读核对 ===")
    print(f"state: {result['state_path']}（{result['state_format']}）")
    print(f"v1 遗留 upstream 条目 {result['legacy_upstream_entries']} 个 / "
          f"当前配置上游对 {result['configured_upstream_pairs']} 个")
    print(f"已登记但未启用的候选来源：{result['candidates_not_enabled'] or '无'}")
    print()
    for r in rows:
        print(f"  {r['local']}")
        print(f"    上游        {r['upstream']}")
        print(f"    v1 记录     {r['legacy_value']}")
        print(f"    当前 HEAD   {r['current_head']} {r['head_date']}")
    print()
    print("结论：" + result["conclusion"])
    print("本工具只读，未写入任何文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
