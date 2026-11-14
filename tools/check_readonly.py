#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态只读守卫（上岗前查工具箱）。

扫描交付包源码，凡出现「对 GitHub 的写操作」「明文飞书 Webhook」「注入器残留」
一律阻断。只允许两条写白名单：
  W1 本仓库 state.json（由 workflow 的 git push 完成）
  W2 飞书 Webhook（notify.py / workflow 的失败告警，目标不是 api.github.com）
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELF = os.path.basename(__file__)

CODE_EXT = (".py", ".yml", ".yaml", ".js", ".json", ".sh")
SKIP_DIRS = {"fixtures", "__pycache__", ".git", "node_modules"}


def _skip_dir(name: str) -> bool:
    # 备份目录（install.py 生成）含历史注入器，只作归档、不参与扫描与提交
    return name in SKIP_DIRS or name.startswith("backup-") or name.startswith("repo-monitor-backup-")

# 拼装正则，避免本文件自身被自己的模式命中
GH_HOST = "api" + ".github" + ".com"
HOOK = "open.feishu.cn/open-apis/bot/v2/hook/"
WRITE_VERBS = ("POST", "PUT", "PATCH", "DELETE")


def _files() -> list[str]:
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
        for f in files:
            if f == SELF or not f.endswith(CODE_EXT):
                continue
            if f.endswith(".md"):
                continue
            out.append(os.path.join(base, f))
    return sorted(out)


def scan() -> dict:
    violations: list[dict] = []
    checked = 0
    for path in _files():
        rel = os.path.relpath(path, ROOT)
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            continue
        checked += 1
        lines = text.splitlines()

        # 1) 对 GitHub API 的非 GET 请求
        for i, ln in enumerate(lines, 1):
            if GH_HOST in ln or ("/repos/" in ln and "api" in ln):
                for verb in WRITE_VERBS:
                    if re.search(rf'\bmethod\s*=\s*["\']{verb}["\']', ln) or \
                       re.search(rf'\brequests?\.{verb.lower()}\s*\(', ln):
                        violations.append({"file": rel, "line": i,
                                           "rule": "对 GitHub API 的非 GET 请求",
                                           "text": ln.strip()[:140]})

        # 2) 明文飞书 Webhook（真实 token 形态）
        m = re.search(re.escape(HOOK) + r"[0-9a-fA-F-]{16,}", text)
        if m:
            violations.append({"file": rel, "line": text[:m.start()].count("\n") + 1,
                               "rule": "硬编码飞书 Webhook，必须改为环境变量",
                               "text": "<redacted>"})

        # 3) 跨仓库写入 / 注入器残留
        for i, ln in enumerate(lines, 1):
            if re.search(r"/repos/[^\s]+/(contents|actions/secrets|hooks|labels|issues)/", ln) and \
               any(v in ln for v in WRITE_VERBS):
                violations.append({"file": rel, "line": i,
                                   "rule": "疑似跨仓库写入路径", "text": ln.strip()[:140]})
            if "deploy_feishu" in ln and "禁止" not in ln and "不进入" not in ln:
                violations.append({"file": rel, "line": i,
                                   "rule": "部署注入器残留", "text": ln.strip()[:140]})
            if "MONITOR_PAT" in ln and "退役" not in ln and "禁止" not in ln:
                violations.append({"file": rel, "line": i,
                                   "rule": "已退役的可写 PAT 仍在配置中",
                                   "text": ln.strip()[:140]})

    return {"checked_files": checked, "violations": violations,
            "passed": len(violations) == 0}


def main() -> int:
    r = scan()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if not r["passed"]:
        print("\n[FAIL] 只读守卫未通过，禁止上线。", file=sys.stderr)
        return 1
    print(f"\n[OK] 只读守卫通过：扫描 {r['checked_files']} 个文件，无违规。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
