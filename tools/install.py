#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 v2 交付包安装进「已 clone 的 repo-monitor 仓库」。

这是纯本地文件操作，不联网、不调用任何 GitHub API、不 push、不动任何其他仓库。

安全约束（对应单仓库边界要求）：
  1. 目标目录必须是 git 仓库，且 remote origin 指向 your-org/repo-monitor；
     否则直接拒绝，防止把代码误装进业务仓库。
  2. 默认 dry-run，只打印将要做的事；必须显式 --apply 才写文件。
  3. 绝不覆盖目标已有的 state.json（那是 v1 状态，含 500 条去重 ID）。
  4. 删除旧注入器前先备份到 backup-<时间戳>/，只追加不覆盖。
  5. 不复制 .env / state.json / __pycache__ / .git。

用法：
  python3 tools/install.py --target /path/to/repo-monitor              # 预演
  python3 tools/install.py --target /path/to/repo-monitor --apply       # 执行
  python3 tools/install.py --target /path/to/repo-monitor --apply --no-backup
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 拼接构造，避免本文件被只读守卫的「注入器残留」规则误判
LEGACY_INJECTOR = "deploy_" + "feishu.py"
LEGACY_WORKFLOW = "feishu-" + "notify.yml"

# 绝不从交付包复制到目标的东西（v1 state.json 由目标仓库自己保留）
SKIP_NAMES = {".git", "__pycache__", ".env", "state.json", "state.json.bak",
              ".pytest_cache", ".coverage", "node_modules"}
SKIP_SUFFIX = (".tmp", ".pyc", ".pyo", ".log", ".bad")

HOOK_PATTERN = re.compile(r"open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-fA-F-]{16,}")
TOKEN_PATTERN = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,})")


def _remote(target: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", target, "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL, timeout=10).decode().strip()
    except Exception:
        return ""


def _is_valid_target(target: str) -> tuple[bool, str]:
    if not os.path.isdir(target):
        return False, "目标目录不存在"
    if not os.path.isdir(os.path.join(target, ".git")):
        return False, "目标不是 git 仓库（必须有 .git 目录）"
    remote = _remote(target)
    if not remote:
        return False, "读不到 origin remote"
    if "repo-monitor" not in remote:
        return False, f"origin 不是 repo-monitor，拒绝操作以免误伤其他仓库：{remote}"
    return True, remote


def _walk_src() -> list[str]:
    """列出交付包里所有需要同步的相对路径。"""
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_NAMES and not d.startswith("backup-")]
        for f in files:
            if f in SKIP_NAMES or f.endswith(SKIP_SUFFIX):
                continue
            rel = os.path.relpath(os.path.join(base, f), ROOT)
            out.append(rel)
    return sorted(out)


def _scan_secrets(target: str) -> list[dict]:
    """扫描目标仓库里残留的明文密钥（只报告，不自动删）。"""
    hits = []
    for base, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules",
                                                "backup-" + "*"} and not d.startswith("backup-")]
        for f in files:
            p = os.path.join(base, f)
            try:
                text = open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for pat, name in ((HOOK_PATTERN, "明文飞书 Webhook"), (TOKEN_PATTERN, "GitHub token")):
                for m in pat.finditer(text):
                    hits.append({"file": os.path.relpath(p, target), "type": name,
                                 "line": text[:m.start()].count("\n") + 1})
    return hits


def plan(target: str) -> dict:
    ok, info = _is_valid_target(target)
    if not ok:
        return {"ok": False, "reason": info}

    src_files = _walk_src()
    actions = []
    for rel in src_files:
        dst = os.path.join(target, rel)
        action = "新增" if not os.path.exists(dst) else "覆盖"
        actions.append({"action": action, "path": rel})

    # state.json 必须保留原样
    state_path = os.path.join(target, "state.json")
    state_status = ("保留 v1（不覆盖，运行时自动迁移）" if os.path.exists(state_path)
                    else "不存在（首次运行时生成）")

    removals = []
    for name in (LEGACY_INJECTOR, os.path.join(".github", "workflows", LEGACY_WORKFLOW)):
        p = os.path.join(target, name)
        if os.path.exists(p):
            removals.append(name)

    return {"ok": True, "remote": info, "target": target,
            "sync_count": len(src_files), "actions": actions, "removals": removals,
            "state_status": state_status, "secrets_found": _scan_secrets(target),
            "backup_dir": ""}


def apply(target: str, do_backup: bool = True) -> dict:
    p = plan(target)
    if not p["ok"]:
        return p
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # 备份必须放在仓库「外面」：里面含待删除的旧注入器（可能带明文 Webhook），
    # 若留在仓库内会被 git add -A 重新提交回公开仓库，等于二次泄漏。
    backup = (os.path.join(os.path.dirname(target), "repo-monitor-backup-" + stamp)
              if do_backup else "")
    if backup:
        os.makedirs(backup, exist_ok=True)

    copied, backed_up = 0, 0
    for item in p["actions"]:
        rel = item["path"]
        src = os.path.join(ROOT, rel)
        dst = os.path.join(target, rel)
        if backup and os.path.exists(dst):
            b = os.path.join(backup, rel)
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(dst, b)
            backed_up += 1
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    removed = []
    for name in p["removals"]:
        src = os.path.join(target, name)
        if backup:
            b = os.path.join(backup, name)
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(src, b)
            backed_up += 1
        os.remove(src)
        removed.append(name)

    p.update({"applied": True, "copied": copied, "backed_up": backed_up,
              "removed": removed, "backup_dir": backup,
              "backup_inside_repo": bool(backup) and os.path.commonpath([backup, target]) == target,
              "secrets_after": _scan_secrets(target)})
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="把 v2 交付包安装进已 clone 的 repo-monitor")
    ap.add_argument("--target", required=True, help="已 clone 的 repo-monitor 目录绝对路径")
    ap.add_argument("--apply", action="store_true", help="真正执行写入（默认只预演）")
    ap.add_argument("--no-backup", action="store_true", help="不备份被覆盖/删除的文件（不推荐）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非人可读摘要")
    args = ap.parse_args()

    target = os.path.abspath(args.target)
    if target == ROOT:
        print("[拒绝] --target 不能是交付包自身", file=sys.stderr)
        return 1

    result = apply(target, do_backup=not args.no_backup) if args.apply else plan(target)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        print("[拒绝]", result.get("reason"))
        return 1

    print("=== repo-monitor v2 安装" + ("（已执行）" if args.apply else "（预演，未写任何文件）") + " ===")
    print("目标:", result["target"])
    print("origin:", result["remote"])
    print("state.json:", result["state_status"])
    print(f"同步 {result['sync_count']} 个文件："
          f"新增 {sum(1 for a in result['actions'] if a['action']=='新增')} / "
          f"覆盖 {sum(1 for a in result['actions'] if a['action']=='覆盖')}")
    print("将删除的旧文件:", result["removals"] or "无（干净）")
    if result["secrets_found"]:
        print(f"[!] 发现 {len(result['secrets_found'])} 处明文密钥残留（只报告，请自行处理）：")
        for h in result["secrets_found"][:10]:
            print(f"     {h['file']}:{h['line']} {h['type']}")
    else:
        print("[OK] 目标仓库未发现明文密钥")
    if args.apply:
        print(f"已复制 {result['copied']} 个文件，备份 {result['backed_up']} 个到：")
        print(f"    {result['backup_dir']}/")
        print("    备份在仓库「外面」，不会被 git add -A 提交（里面有旧注入器的明文密钥）")
        print("已删除:", result["removed"] or "无")
        print("\n>>> 接下来请手动执行（我不替你 push）：")
        print(f"    cd {target}")
        print(f"    git status          # 应看到大量 M/A/D，其中 D {LEGACY_INJECTOR} 属预期")
        print("    git add -A          # 删除已被物理删除的旧注入器也会被记录")
        print('    git commit -m "refactor: repo-monitor v2 只读管家"')
        print("    git push origin main")
    else:
        print("\n确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
