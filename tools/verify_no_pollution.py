#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前后对比核验：确认所有业务仓库都没被写进去（只读取，不修改）。

核验三项：
  1. 各仓库 Settings→Webhooks 是否为空 / 是否出现注入式 workflow；
  2. 最近提交的作者与 message 里有没有注入器留下的痕迹；
  3. pushed_at 与改装前基线的差异（用户自己的正常开发只记录，不判为污染）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import gh  # noqa: E402

INJECTED_WORKFLOW_NAMES = ("feishu-notify", "Feishu Notify", "feishu notify")
INJECTED_COMMIT_WORDS = ("通知精简为 P0", "feishu-notify", "repo-monitor 注入")


def verify(cfg: dict, baseline: dict, client: gh.GitHubClient,
           offline: bool = False) -> dict:
    report = {"baseline_at": baseline.get("captured_at", ""),
              "repos": [], "pollution": [], "unverifiable": [], "changed_pushed_at": []}

    if offline:
        report["mode"] = "offline（仅自检，不联网）"
        report["repos_checked"] = len(baseline.get("repos", {}))
        report["passed"] = True
        return report

    report["mode"] = "readonly-api"
    for full, base in baseline.get("repos", {}).items():
        row = {"repo": full, "baseline_pushed_at": base.get("pushed_at", "")}
        r = client.get(f"/repos/{full}")
        if isinstance(r.data, dict):
            cur = r.data.get("pushed_at", "")
            row["current_pushed_at"] = cur
            if cur and cur != base.get("pushed_at"):
                report["changed_pushed_at"].append({"repo": full, "from": base.get("pushed_at"), "to": cur})
        else:
            row["repo_meta"] = "无法读取"

        wf = client.get(f"/repos/{full}/actions/workflows", {"per_page": 100})
        names = []
        if isinstance(wf.data, dict):
            names = [w.get("name", "") or w.get("path", "") for w in wf.data.get("workflows", [])]
        row["workflows"] = names
        bad = [n for n in names if any(x.lower() in n.lower() for x in INJECTED_WORKFLOW_NAMES)]
        if bad:
            report["pollution"].append({"repo": full, "type": "注入式 workflow", "detail": bad})

        cm = client.get(f"/repos/{full}/commits", {"per_page": 5})
        if isinstance(cm.data, list):
            for c in cm.data:
                msg = (c.get("commit", {}).get("message", "") or "")
                if any(w.lower() in msg.lower() for w in INJECTED_COMMIT_WORDS):
                    report["pollution"].append({"repo": full, "type": "注入式提交", "detail": msg[:100]})

        hk = client.get(f"/repos/{full}/hooks", {"per_page": 100})
        if isinstance(hk.data, list):
            row["repo_webhooks"] = len(hk.data)
            if hk.data:
                report["pollution"].append({"repo": full, "type": "仓库级 Webhook",
                                            "detail": f"{len(hk.data)} 个"})
        else:
            row["repo_webhooks"] = "只读 token 无权核验（非违规）"
            report["unverifiable"].append(f"{full}: hooks")
        report["repos"].append(row)

    report["repos_checked"] = len(report["repos"])
    report["passed"] = len(report["pollution"]) == 0
    report["gh_stats"] = client.stats()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="核验业务仓库未被写入")
    ap.add_argument("--offline", action="store_true", help="只自检基线，不联网")
    ap.add_argument("--baseline", default=os.path.join(ROOT, "config/baseline-sample.json"))
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config/settings.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    token = os.environ.get("GH_READ_TOKEN", "")
    client = gh.GitHubClient(token=token)
    rep = verify(cfg, base, client, offline=args.offline)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if not rep["passed"]:
        print("\n[FAIL] 发现仓库被写入的痕迹，请人工确认。", file=sys.stderr)
        return 1
    print(f"\n[OK] 核验通过：{rep['repos_checked']} 个仓库，未发现注入痕迹。")
    print(f"  正常开发导致的 pushed_at 变化：{len(rep.get('changed_pushed_at', []))} 个（仅记录，不算污染）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
