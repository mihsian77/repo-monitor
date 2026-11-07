#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规范化事件模型与三种数据源。

统一 ID 是幂等的基石：同一对象无论来自 events 流、REST 端点还是 Webhook，
都生成同一个语义 ID（如 issue:{repo}:{number}），重复到达自动收敛。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import gh


@dataclass
class Event:
    id: str
    kind: str
    repo: str
    actor: str = "?"
    created_at: str = ""
    title: str = ""
    body: str = ""
    url: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "repo": self.repo,
                "actor": self.actor, "created_at": self.created_at,
                "title": self.title, "body": self.body, "url": self.url,
                "meta": self.meta}

    @staticmethod
    def from_dict(d: dict) -> "Event":
        return Event(id=d.get("id", ""), kind=d.get("kind", ""),
                     repo=d.get("repo", ""), actor=d.get("actor", "?"),
                     created_at=d.get("created_at", ""), title=d.get("title", ""),
                     body=d.get("body", ""), url=d.get("url", ""),
                     meta=d.get("meta", {}) or {})


def _num(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1]


def _upstream_pairs(cfg: dict):
    """展开上游映射。value 可以是单个 "owner/repo"，也可以是多个来源的 list。

    一个本地仓库可能有不止一个参考来源（例如 app-console 同时参考两个上游），
    旧版 dict 只支持一对一，这里统一展开成 (local, upstream) 序列。
    """
    for local, val in (cfg.get("upstream", {}) or {}).items():
        sources = val if isinstance(val, (list, tuple)) else [val]
        for up in sources:
            if isinstance(up, str) and up.count("/") == 1:
                yield local, up


class ReplaySource:
    """用真实采集数据回放，验证分级/聚合/待办逻辑，不需要网络与凭证。"""

    def __init__(self, path: str):
        self.path = path
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.captured_at = raw.get("captured_at", "")
        self.events = [Event.from_dict(d) for d in raw.get("events", [])]

    def collect(self, state: dict, cfg: dict) -> list[Event]:
        return list(self.events)


class GitHubSource:
    """只读轮询采集器。所有请求经由 GitHubClient（强制 GET）。"""

    def __init__(self, client: gh.GitHubClient, cfg: dict, state: dict):
        self.c = client
        self.cfg = cfg
        self.state = state
        self.repos = list(cfg.get("repos", [])) + list(cfg.get("extra_repos", []))

    # 水位回退：GitHub 的 since 是「严格晚于」，而 commits / issues / comments
    # 各自按不同时间字段过滤。回退 60 秒，配合 30 分钟一轮的节奏，既避免同一秒
    # 的事件被边界吞掉，也不会撑爆 100 条窗口；重复项由 deliveries 幂等账收敛。
    # 水位不变时请求 URL 稳定，ETag 304 仍然生效，不额外耗配额。
    SINCE_LAG_SECONDS = 60

    def _since(self, key: str) -> str:
        raw = self.state["watermarks"].get(key, "")
        if not raw:
            return ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return (dt - timedelta(seconds=self.SINCE_LAG_SECONDS)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            return ""

    def collect(self, state: dict, cfg: dict) -> list[Event]:
        out: list[Event] = []
        seen: set[str] = set()

        def add(e: Event):
            if e.id and e.id not in seen:
                seen.add(e.id)
                out.append(e)

        # 1) 仓库清单（ETag，304 不耗配额）
        resp = self.c.get(f"/users/{cfg['owner']}/repos",
                          {"per_page": 100, "type": "owner", "sort": "updated"})
        live = {}
        if isinstance(resp.data, list):
            live = {r["full_name"]: r for r in resp.data}
            for full, r in live.items():
                add(Event(f"repo:{full}:{r.get('id')}", "repo_snapshot", full,
                          cfg["owner"], r.get("updated_at", ""),
                          meta={"forks": r.get("forks_count", 0),
                                "stars": r.get("stargazers_count", 0),
                                "pushed_at": r.get("pushed_at", ""),
                                "default_branch": r.get("default_branch", "main")}))
        # 关键修复：空列表不代表「账号没仓库」，可能是临时 API 故障，绝不据此清基线
        if resp.error and not live:
            self.c.errors.append("仓库清单获取失败：本轮不更新 known_repos / upstream 基线")

        # 2) 逐仓事件面
        for full in self.repos:
            r = live.get(full, {})
            branch = r.get("default_branch") or cfg.get("default_branches", {}).get(full, "main")

            # 2.1 事件流（ETag + X-Poll-Interval）
            ev_resp = self.c.get(f"/repos/{full}/events", {"per_page": 100})
            if isinstance(ev_resp.data, list):
                for e in ev_resp.data:
                    out.extend(_event_from_events_api(e, full))

            # 2.2 新提交（补 v1 的死字段 last_push）
            since = self._since(f"commits:{full}")
            params = {"per_page": 100, "sha": branch}
            if since:
                params["since"] = since
            cr = self.c.get(f"/repos/{full}/commits", params)
            if isinstance(cr.data, list):
                for c in cr.data[:100]:
                    files = []
                    # 单条 commit 详情带 files，用于「关键提交」判定
                    if len(cr.data) <= 20:
                        det = self.c.get(f"/repos/{full}/commits/{c.get('sha','')}")
                        if isinstance(det.data, dict):
                            files = [f.get("filename", "") for f in det.data.get("files", [])]
                    msg = (c.get("commit", {}).get("message", "") or "").split("\n")[0]
                    add(Event(f"commit:{full}:{c.get('sha','')}", "push", full,
                              (c.get("author") or {}).get("login")
                              or (c.get("commit", {}).get("author", {}) or {}).get("name", "?"),
                              c.get("commit", {}).get("author", {}).get("date", ""),
                              title=msg, url=c.get("html_url", ""),
                              meta={"files": files, "sha": c.get("sha", ""),
                                    "source": "commits_api"}))

            # 2.3 Issue / PR（按 updated_at 捞「老 issue 有新评论」，不再用 created > last_check 误杀）
            # 各端点用各自的水位，互不拖累：提交水位不决定 Issue 的取数范围。
            since_issues = self._since(f"issues:{full}")
            iss = self.c.get(f"/repos/{full}/issues",
                             {"state": "all", "sort": "updated", "per_page": 100,
                              "since": since_issues or None})
            if isinstance(iss.data, list):
                for it in iss.data:
                    kind = "pr" if it.get("pull_request") else "issue"
                    add(Event(f"{kind}:{full}:{it.get('number')}", kind, full,
                              (it.get("user") or {}).get("login", "?"),
                              it.get("created_at", ""), title=it.get("title", ""),
                              body=(it.get("body") or "")[:800], url=it.get("html_url", ""),
                              meta={"number": it.get("number"), "state": it.get("state"),
                                    "updated_at": it.get("updated_at", ""),
                                    "comments": it.get("comments", 0)}))

            # 2.4 评论正文（补 v1 最大盲区）
            since_comments = self._since(f"comments:{full}")
            cs = self.c.get(f"/repos/{full}/issues/comments",
                            {"per_page": 100, "since": since_comments or None})
            if isinstance(cs.data, list):
                for c in cs.data:
                    add(Event(f"comment:{full}:{c.get('id')}", "comment", full,
                              (c.get("user") or {}).get("login", "?"),
                              c.get("created_at", ""), body=(c.get("body") or "")[:800],
                              url=c.get("html_url", ""),
                              meta={"issue_number": _num(c.get("issue_url", "")),
                                    "updated_at": c.get("updated_at", "")}))

            # 2.5 Release（一批多个全拿到，不再只取 latest）
            rr = self.c.get(f"/repos/{full}/releases", {"per_page": 100})
            if isinstance(rr.data, list):
                for rel in rr.data[:20]:
                    add(Event(f"release:{full}:{rel.get('id')}", "release", full,
                              (rel.get("author") or {}).get("login", "?"),
                              rel.get("published_at") or rel.get("created_at", ""),
                              title=rel.get("name") or rel.get("tag_name", ""),
                              url=rel.get("html_url", ""),
                              meta={"tag": rel.get("tag_name", ""),
                                    "draft": bool(rel.get("draft"))}))

            # 2.6 CI 运行（失败 + 恢复）
            runs = self.c.get(f"/repos/{full}/actions/runs", {"per_page": 30})
            if isinstance(runs.data, dict):
                for run in runs.data.get("workflow_runs", []):
                    concl = run.get("conclusion")
                    add(Event(f"run:{full}:{run.get('id')}",
                              "run_failure" if concl == "failure" else "run_other",
                              full, (run.get("actor") or {}).get("login", "?"),
                              run.get("created_at", ""), title=run.get("name", ""),
                              url=run.get("html_url", ""),
                              meta={"conclusion": concl, "workflow": run.get("name", ""),
                                    "branch": run.get("head_branch", ""),
                                    "head_commit": ((run.get("head_commit") or {}).get("message") or "")[:120],
                                    "jobs_url": run.get("jobs_url", "")}))

        # 3) 通知（all=true 才不会漏掉已读的评论/提及）
        nr = self.c.get("/notifications", {"all": "true", "per_page": 100,
                                          "since": self._since("notifications") or None})
        if isinstance(nr.data, list):
            for n in nr.data:
                subj = n.get("subject", {}) or {}
                add(Event(f"notification:{n.get('id')}", "notification",
                          (n.get("repository") or {}).get("full_name", "?"),
                          "?", n.get("updated_at", ""), title=subj.get("title", ""),
                          url=subj.get("latest_comment_url", "") or subj.get("url", ""),
                          meta={"reason": n.get("reason", ""), "type": subj.get("type", ""),
                                "unread": n.get("unread", False)}))

        # 4) 上游雷达：真 compare（只读 GET），失败自动降级，绝不假装拿到全量。
        #    compare 返回 commits<=250 / files<=300，超出必须标 truncated，
        #    不能把截断结果当完整列表去判「敏感路径没命中」。
        for local, upstream in _upstream_pairs(cfg):
            e = self._upstream_event(local, upstream, cfg)
            if e:
                add(e)

        # 去重收敛
        uniq: dict[str, Event] = {}
        for e in out:
            uniq.setdefault(e.id, e)
        return sorted(uniq.values(), key=lambda x: x.created_at or "")

    def _upstream_event(self, local: str, upstream: str, cfg: dict) -> Event | None:
        """单个上游的雷达事件。三级降级，全部只读 GET：

          L1 compare  → 落后量 ahead_by/behind_by + 完整（或明确截断的）files
          L2 commit 详情 → 只有该提交的 files，仍可判敏感路径
          L3 都拿不到 → files_status=unavailable，规则层必须如实说「无法判定」，
                        绝不静默当成「没命中敏感路径」

        事件 ID 沿用 upstream:{local}:{sha[:12]}，上游 sha 不变就不重复通知。
        """
        lb = cfg.get("default_branches", {}).get(local, "main")
        cr = self.c.get(f"/repos/{upstream}/commits", {"per_page": 1})
        if not (isinstance(cr.data, list) and cr.data):
            return None
        c = cr.data[0]
        sha = c.get("sha", "")
        msg = (c.get("commit", {}).get("message", "") or "").split("\n")[0]
        actor = ((c.get("author") or {}).get("login")
                 or (c.get("commit", {}).get("author", {}) or {}).get("name", "?"))
        date = c.get("commit", {}).get("author", {}).get("date", "")
        meta: dict[str, Any] = {
            "upstream": upstream, "sha": sha, "local_branch": lb,
            "compare_status": "unavailable", "files": [],
            "files_truncated": False, "files_status": "unavailable",
            "source": "upstream",
        }

        # L1: 跨 fork compare。base 用上游 sha，head 用 owner:branch。
        local_owner = local.split("/")[0] if "/" in local else cfg.get("owner", "")
        if sha and local_owner:
            cp = self.c.get(f"/repos/{upstream}/compare/{sha}...{local_owner}:{lb}",
                            use_etag=False)
            parsed = parse_upstream_compare(cp.data)
            if parsed:
                meta.update(parsed)

        # L2: compare 不可用（非 fork / 404 / 限流）时，至少拿到该提交的文件清单
        if meta.get("compare_status") != "ok" and sha:
            det = self.c.get(f"/repos/{upstream}/commits/{sha}", use_etag=False)
            if isinstance(det.data, dict):
                fs = [f.get("filename", "") for f in (det.data.get("files") or [])
                      if isinstance(f, dict) and f.get("filename")]
                if fs:
                    meta.update({"files": fs[:300], "files_truncated": len(fs) > 300,
                                 "files_status": "ok", "compare_status": "commit_detail"})

        return Event(f"upstream:{local}:{sha[:12]}", "upstream", local, actor, date,
                     title=msg, url=c.get("html_url", ""), meta=meta)


def parse_upstream_compare(data: Any) -> dict:
    """把 GitHub compare 响应压成事件 meta。纯函数，离线可测。

    compare 的 commits/files 有硬上限（社区实测 250 / 300），超出必须标记
    files_truncated=True，否则「没命中敏感路径」是假阴性。
    """
    if not isinstance(data, dict) or "ahead_by" not in data:
        return {}
    files = [f.get("filename", "") for f in (data.get("files") or [])
             if isinstance(f, dict) and f.get("filename")]
    commits = data.get("commits") or []
    total = data.get("total_commits")
    truncated = bool(len(files) >= 300 or len(commits) >= 250
                     or (isinstance(total, int) and total > len(commits)))
    # 语义严格区分：只有真的拿到文件清单才是 ok；
    # 列表被截断时「没命中」不成立，只能说「已知命中，可能还有更多」。
    return {
        "compare_status": "ok",
        "ahead_by": data.get("ahead_by"), "behind_by": data.get("behind_by"),
        "total_commits": total, "status": data.get("status", ""),
        "files": files[:300], "files_truncated": truncated,
        "files_status": "ok" if files else "unavailable",
        "commits_seen": len(commits),
    }


def _event_from_events_api(e: dict, full: str) -> list[Event]:
    """把 /events 的 PushEvent 等转成规范事件（该 payload 不含 commit message/文件列表，
    仅作兜底；真正的关键提交判定依赖 commits API / Webhook）。"""
    t = e.get("type", "")
    p = e.get("payload", {}) or {}
    a = (e.get("actor") or {}).get("login", "?")
    ts = e.get("created_at", "")
    rid = e.get("id", "")
    out: list[Event] = []
    if t == "PushEvent":
        msgs = [c.get("message", "").split("\n")[0] for c in (p.get("commits") or [])]
        out.append(Event(f"push:{full}:{rid}", "push", full, a, ts,
                         title=msgs[0] if msgs else "",
                         meta={"ref": p.get("ref", ""), "size": p.get("size", 0),
                               "messages": msgs[:20], "files": [], "source": "events"}))
    elif t == "IssuesEvent":
        it = p.get("issue", {}) or {}
        out.append(Event(f"issue:{full}:{it.get('number', rid)}", "issue", full, a, ts,
                         title=it.get("title", ""), url=it.get("html_url", ""),
                         meta={"action": p.get("action", ""), "source": "events"}))
    elif t == "IssueCommentEvent":
        c = p.get("comment", {}) or {}
        out.append(Event(f"comment:{full}:{c.get('id', rid)}", "comment", full, a, ts,
                         body=(c.get("body") or "")[:800], url=c.get("html_url", ""),
                         meta={"source": "events"}))
    elif t == "PullRequestEvent":
        pr = p.get("pull_request", {}) or {}
        out.append(Event(f"pr:{full}:{pr.get('number', rid)}", "pr", full, a, ts,
                         title=pr.get("title", ""), url=pr.get("html_url", ""),
                         meta={"action": p.get("action", ""), "source": "events"}))
    elif t == "ReleaseEvent":
        rel = p.get("release", {}) or {}
        out.append(Event(f"release:{full}:{rel.get('id', rid)}", "release", full, a, ts,
                         title=rel.get("name") or rel.get("tag_name", ""),
                         url=rel.get("html_url", ""),
                         meta={"tag": rel.get("tag_name", ""), "source": "events"}))
    elif t in ("CreateEvent", "DeleteEvent"):
        out.append(Event(f"ref:{full}:{rid}", "branch_tag", full, a, ts,
                         meta={"action": "create" if t == "CreateEvent" else "delete",
                               "ref": p.get("ref", ""), "ref_type": p.get("ref_type", "")}))
    elif t in ("WatchEvent", "ForkEvent", "PublicEvent", "MemberEvent"):
        out.append(Event(f"social:{full}:{rid}", "social", full, a, ts,
                         meta={"event_type": t}))
    return out


class WebhookSource:
    """解析 GitHub App 集中式 Webhook 的投递（供对账/回放使用）。

    delivery_id 即 X-GitHub-Delivery，天然全局唯一，直接作为幂等键。
    """

    def __init__(self, event_name: str, payload: dict, delivery_id: str = ""):
        self.event_name = event_name
        self.payload = payload or {}
        self.delivery_id = delivery_id

    def collect(self, state: dict, cfg: dict) -> list[Event]:
        p = self.payload
        repo = (p.get("repository") or {}).get("full_name", "?")
        actor = (p.get("sender") or {}).get("login", "?")
        dic = {"delivery": self.delivery_id}

        def ev(kind, id_suffix, ts="", **kw):
            eid = (f"wh:{self.delivery_id}:{id_suffix}" if self.delivery_id
                   else f"wh:{kind}:{id_suffix}")
            m = dict(dic)
            if "meta" in kw:
                m.update(kw.pop("meta"))
            return Event(eid, kind, repo, actor, ts, meta=m, **kw)

        n = self.event_name
        if n == "push":
            cs = p.get("commits", []) or []
            files: list[str] = []
            for c in cs:
                files += (c.get("added") or []) + (c.get("modified") or []) + (c.get("removed") or [])
            return [ev("push", p.get("after", "")[:12],
                       (p.get("head_commit") or {}).get("timestamp", ""),
                       title=(cs[-1].get("message", "") if cs else "").split("\n")[0],
                       url=p.get("compare", ""),
                       meta={"files": list(dict.fromkeys(files)),
                             "messages": [c.get("message", "").split("\n")[0] for c in cs],
                             "ref": p.get("ref", ""),
                             "size": p.get("size", len(cs))})]
        if n == "issues":
            it = p.get("issue", {}) or {}
            return [ev("issue", f"{it.get('number')}:{p.get('action')}", it.get("created_at", ""),
                       title=it.get("title", ""), body=(it.get("body") or "")[:800],
                       url=it.get("html_url", ""))]
        if n == "issue_comment":
            c = p.get("comment", {}) or {}
            return [ev("comment", c.get("id", ""), c.get("created_at", ""),
                       body=(c.get("body") or "")[:800], url=c.get("html_url", ""))]
        if n in ("pull_request_review", "pull_request_review_comment"):
            r = p.get("review", {}) or p.get("comment", {}) or {}
            return [ev("review", r.get("id", ""), r.get("submitted_at") or r.get("created_at", ""),
                       body=(r.get("body") or "")[:800], url=r.get("html_url", ""))]
        if n == "release":
            rel = p.get("release", {}) or {}
            return [ev("release", rel.get("id", ""),
                       rel.get("published_at") or rel.get("created_at", ""),
                       title=rel.get("name") or rel.get("tag_name", ""),
                       url=rel.get("html_url", ""))]
        if n in ("workflow_run", "check_run", "workflow_job"):
            wr = p.get("workflow_run") or p.get("check_run") or p.get("workflow_job") or {}
            concl = wr.get("conclusion", "")
            return [ev("run_failure" if concl == "failure" else "run_other", wr.get("id", ""),
                       wr.get("created_at", ""), title=wr.get("name", ""),
                       url=wr.get("html_url", ""))]
        if n in ("dependabot_alert", "secret_scanning_alert", "code_scanning_alert",
                 "repository_vulnerability_alert"):
            a = p.get("alert", {}) or {}
            return [ev("security", a.get("number", p.get("action", "")), "",
                       title=f"{n}: {p.get('action','')}", url=a.get("html_url", ""))]
        if n == "branch_protection_rule":
            return [ev("protection", p.get("action", ""), "", title="分支保护规则变更")]
        if n in ("create", "delete"):
            return [ev("branch_tag", p.get("ref", ""), "",
                       meta={**dic, "action": n, "ref": p.get("ref", ""),
                             "ref_type": p.get("ref_type", "")})]
        if n in ("fork", "star", "watch", "public"):
            return [ev("social", p.get("action", "x"), "")]
        return []
