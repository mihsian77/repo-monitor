#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一只读 GitHub 客户端。

运行时守卫：本模块只构造 GET 请求，代码层面不可能对业务仓库发出写操作。
另外提供 ETag 条件请求（304 不消耗配额）、限流退避、X-Poll-Interval 尊重、
以及 302 跳转时剥离 Authorization 头（沿用 v1 里处理得很专业的那一段）。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

API_BASE = "https://api.github.com"
_ALLOWED_METHODS = frozenset({"GET"})


class ReadOnlyViolation(RuntimeError):
    """试图对 GitHub 发出非 GET 请求时抛出。"""


def assert_readonly(method: str) -> None:
    """所有出站请求的唯一入口检查。非 GET 立即抛错并中断本轮。"""
    if method.upper() not in _ALLOWED_METHODS:
        raise ReadOnlyViolation(
            f"拒绝非只读请求：{method}。本管家对所有业务仓库只有 GET。"
        )


@dataclass
class Response:
    data: Any = None
    status: int = 0
    not_modified: bool = False
    etag: str = ""
    poll_interval: int | None = None
    rate_remaining: int | None = None
    error: str = ""


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub 日志接口 302 跳到带签名的存储 URL，跟随重定向必须去掉
    Authorization 头，否则签名校验失败(403)。这段逻辑来自 v1，予以保留。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newreq = super().redirect_request(req, fp, code, msg, headers, newurl)
        if newreq:
            newreq.headers = {k: v for k, v in newreq.headers.items()
                              if k.lower() != "authorization"}
            newreq.unredirected_hdrs = {k: v for k, v in newreq.unredirected_hdrs.items()
                                        if k.lower() != "authorization"}
        return newreq


class GitHubClient:
    """只读客户端。token 为空时仍可访问公开数据（限额 60/h），仅用于本地 dry-run。"""

    def __init__(self, token: str = "", api_base: str = API_BASE,
                 etag_store: dict | None = None, max_retries: int = 2):
        self.token = token or os.environ.get("GH_READ_TOKEN", "")
        self.api_base = api_base.rstrip("/")
        self.etags: dict[str, str] = etag_store if etag_store is not None else {}
        self.max_retries = max_retries
        self.calls = 0
        self.not_modified = 0
        self.errors: list[str] = []
        self._opener = urllib.request.build_opener(NoAuthRedirect)

    def _headers(self, accept: str, etag: str = "") -> dict:
        h = {
            "Accept": accept,
            "User-Agent": "repo-monitor/2.0 (read-only)",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if etag:
            h["If-None-Match"] = etag
        return h

    def _url(self, path: str, params: dict | None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            base = path
        else:
            base = self.api_base + "/" + path.lstrip("/")
        if params:
            sep = "&" if "?" in base else "?"
            base += sep + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        return base

    def get(self, path: str, params: dict | None = None,
            accept: str = "application/vnd.github+json",
            timeout: int = 30, use_etag: bool = True) -> Response:
        """唯一的数据出口。强制 GET；带 ETag；遵守限流与轮询间隔。"""
        assert_readonly("GET")
        url = self._url(path, params)
        etag = self.etags.get(url, "") if use_etag else ""
        delay = 1.0

        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers=self._headers(accept, etag),
                                         method="GET")
            try:
                self.calls += 1
                with self._opener.open(req, timeout=timeout) as r:
                    raw = r.read().decode("utf-8", errors="replace")
                    hdr = {k.lower(): v for k, v in r.headers.items()}
                    new_etag = hdr.get("etag", "")
                    if new_etag:
                        self.etags[url] = new_etag
                    poll = hdr.get("x-poll-interval")
                    rem = hdr.get("x-ratelimit-remaining")
                    return Response(
                        data=json.loads(raw) if raw.strip() else None,
                        status=r.status, etag=new_etag,
                        poll_interval=int(poll) if poll and poll.isdigit() else None,
                        rate_remaining=int(rem) if rem and rem.isdigit() else None,
                    )
            except urllib.error.HTTPError as e:
                hdr = {k.lower(): v for k, v in (e.headers or {}).items()}
                if e.code == 304:
                    self.not_modified += 1
                    rem = hdr.get("x-ratelimit-remaining")
                    return Response(not_modified=True, status=304, etag=etag,
                                    rate_remaining=int(rem) if rem and rem.isdigit() else None)
                if e.code in (403, 429):
                    # 主限流按 reset 退避；次级限流按 retry-after，否则指数退避
                    retry_after = hdr.get("retry-after")
                    reset = hdr.get("x-ratelimit-reset")
                    if retry_after and retry_after.isdigit():
                        wait = float(retry_after)
                    elif reset and reset.isdigit():
                        wait = max(1.0, float(reset) - time.time())
                    else:
                        wait = min(delay, 8.0)  # 次级限流：短退避
                        delay *= 2
                    # 主限流通常要等到整点重置，可能几十分钟。绝不死等：
                    # 超过 30s 就跳过该端点，本轮照常跑完，下一轮 cron 自然恢复。
                    if wait > 30:
                        msg = (f"限流 {e.code}: 需等待 {wait:.0f}s（>30s），"
                               f"本轮跳过该端点，交由下一轮恢复: {url}")
                        self.errors.append(msg)
                        return Response(status=e.code, error=msg)
                    self.errors.append(f"限流 {e.code}: {url} 短退避 {wait:.0f}s")
                    if attempt < self.max_retries:
                        time.sleep(wait)
                        continue
                    return Response(status=e.code, error="限流重试耗尽")
                if e.code == 404:
                    return Response(status=404, error=f"404 {url}")
                return Response(status=e.code, error=f"HTTP {e.code} {url}")
            except Exception as e:  # 网络抖动
                msg = f"请求异常 {url}: {e}"
                self.errors.append(msg)
                if attempt < self.max_retries:
                    time.sleep(min(delay, 5.0))
                    delay *= 2
                    continue
                return Response(error=msg)
        return Response(error="重试耗尽")

    def get_text(self, path: str, timeout: int = 90) -> str:
        """拉取构建日志（文本），302 去认证头。"""
        assert_readonly("GET")
        url = self._url(path, None)
        req = urllib.request.Request(url, headers=self._headers("application/vnd.github+json"),
                                     method="GET")
        try:
            self.calls += 1
            with self._opener.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            self.errors.append(f"日志获取失败 {url}: {e}")
            return ""

    def paginate(self, path: str, params: dict | None = None,
                 max_pages: int = 10) -> list:
        """按页拉取，per_page 上限 100。响应为 list 时自动翻页。"""
        out: list = []
        p = dict(params or {})
        p.setdefault("per_page", 100)
        for page in range(1, max_pages + 1):
            p["page"] = page
            resp = self.get(path, p, use_etag=False)
            if resp.error or not isinstance(resp.data, list):
                break
            out.extend(resp.data)
            if len(resp.data) < p["per_page"]:
                break
        return out

    def stats(self) -> dict:
        return {"calls": self.calls, "304_saved": self.not_modified,
                "errors": self.errors[:10], "rate_remaining_hint": "见每次响应头"}
