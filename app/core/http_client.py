"""安全 HTTP 客户端：限流、重试、SSRF 防护、体积限制、获取留痕。

设计约束（对应开发方案 §9 安全）：
- 禁止下载器访问内网地址（默认关闭私有地址访问）；
- 限制文件大小与解析时间；
- 每个请求记录来源、耗时、状态，供数据源健康监控使用。
"""

from __future__ import annotations

import ipaddress
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from app.config import settings


class FetchError(RuntimeError):
    """数据获取失败。携带阶段与原因，供报告标注缺口。"""

    def __init__(self, message: str, *, url: str = "", stage: str = "", cause: Exception | None = None):
        super().__init__(message)
        self.url = url
        self.stage = stage
        self.cause = cause


@dataclass
class FetchRecord:
    url: str
    stage: str
    ok: bool
    status_code: int | None = None
    elapsed_ms: int = 0
    bytes: int = 0
    attempts: int = 0
    error: str | None = None
    host: str = ""


@dataclass
class _Bucket:
    rps: float
    last: float = 0.0


class RateLimiter:
    """按主机限流的简易令牌间隔控制，线程安全。"""

    def __init__(self, rps: float):
        self.rps = max(0.1, rps)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def acquire(self, host: str) -> None:
        while True:
            with self._lock:
                bucket = self._buckets.setdefault(host, _Bucket(self.rps))
                now = time.monotonic()
                wait = bucket.last + (1.0 / self.rps) - now
                if wait <= 0:
                    bucket.last = now
                    return
            time.sleep(wait + random.uniform(0, 0.01))


limiter = RateLimiter(settings.per_host_rps)

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

ALLOWED_SCHEMES = {"http", "https"}


def assert_safe_url(url: str) -> str:
    """校验 URL 合法性，默认阻断内网与非法协议。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError(f"非安全协议: {parsed.scheme}", url=url, stage="guard")
    host = parsed.hostname
    if not host:
        raise FetchError("缺少主机名", url=url, stage="guard")
    if settings.allow_private_address:
        return url
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"域名解析失败: {host}", url=url, stage="guard", cause=exc) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _PRIVATE_NETS):
            raise FetchError(f"拒绝访问内网地址: {host} -> {ip}", url=url, stage="guard")
    return url


class HttpClient:
    """带限流、重试和留痕的 HTTP 客户端。"""

    def __init__(self, records: list[FetchRecord] | None = None):
        self.records: list[FetchRecord] = records if records is not None else []
        self._client = httpx.Client(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _record(self, rec: FetchRecord) -> FetchRecord:
        self.records.append(rec)
        return rec

    def request(
        self,
        method: str,
        url: str,
        *,
        stage: str = "",
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        assert_safe_url(url)
        host = urlparse(url).hostname or ""
        attempts = 0
        max_attempts = max(1, retries if retries is not None else settings.http_retries)
        last_error: Exception | None = None

        while attempts < max_attempts:
            attempts += 1
            limiter.acquire(host)
            started = time.monotonic()
            try:
                resp = self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json_body,
                    headers=headers,
                    timeout=timeout or settings.http_timeout,
                )
                elapsed = int((time.monotonic() - started) * 1000)
                rec = FetchRecord(
                    url=url,
                    stage=stage,
                    ok=resp.status_code < 400,
                    status_code=resp.status_code,
                    elapsed_ms=elapsed,
                    bytes=len(resp.content),
                    attempts=attempts,
                    host=host,
                )
                if resp.status_code >= 400:
                    rec.error = f"HTTP {resp.status_code}"
                    if resp.status_code in (429, 500, 502, 503, 504) and attempts < max_attempts:
                        self._record(rec)
                        time.sleep(settings.http_backoff * attempts)
                        continue
                    self._record(rec)
                    raise FetchError(f"HTTP {resp.status_code} {url}", url=url, stage=stage)
                self._record(rec)
                return resp
            except FetchError:
                raise
            except Exception as exc:  # 网络层异常
                last_error = exc
                elapsed = int((time.monotonic() - started) * 1000)
                self._record(
                    FetchRecord(
                        url=url,
                        stage=stage,
                        ok=False,
                        elapsed_ms=elapsed,
                        attempts=attempts,
                        error=type(exc).__name__ + ": " + str(exc)[:200],
                        host=host,
                    )
                )
                if attempts < max_attempts:
                    time.sleep(settings.http_backoff * attempts)
                    continue
        raise FetchError(f"请求失败: {url} ({last_error})", url=url, stage=stage, cause=last_error)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = self.request("GET", url, **kwargs)
        try:
            return resp.json()
        except Exception as exc:
            raise FetchError(f"响应不是合法 JSON: {url}", url=url, stage=kwargs.get("stage", ""), cause=exc) from exc

    def download(self, url: str, dest, *, stage: str = "", referer: str | None = None) -> Path:
        """流式下载，限制体积，写入目标路径。"""
        import os

        assert_safe_url(url)
        dest = Path(dest)
        if not dest.parent.exists():
            dest.parent.mkdir(parents=True)
        host = urlparse(url).hostname or ""
        headers = dict(headers_extra := ({"Referer": referer} if referer else {}))
        limiter.acquire(host)
        started = time.monotonic()
        written = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with self._client.stream(
                "GET", url, headers=headers, timeout=settings.download_timeout
            ) as resp:
                if resp.status_code >= 400:
                    self._record(
                        FetchRecord(url=url, stage=stage, ok=False, status_code=resp.status_code,
                                    error=f"HTTP {resp.status_code}", host=host, attempts=1)
                    )
                    raise FetchError(f"下载失败 HTTP {resp.status_code}: {url}", url=url, stage=stage)
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(65536):
                        written += len(chunk)
                        if written > settings.max_file_bytes:
                            raise FetchError(
                                f"文件超过体积上限 {settings.max_file_bytes} 字节: {url}",
                                url=url,
                                stage=stage,
                            )
                        fh.write(chunk)
            os.replace(tmp, dest)
            self._record(
                FetchRecord(
                    url=url, stage=stage, ok=True, status_code=200,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    bytes=written, attempts=1, host=host,
                )
            )
            return dest
        except FetchError:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            self._record(
                FetchRecord(url=url, stage=stage, ok=False, error=str(exc)[:200],
                            bytes=written, attempts=1, host=host)
            )
            raise FetchError(f"下载异常: {url}", url=url, stage=stage, cause=exc) from exc


def host_of(url: str) -> str:
    return urlparse(url).hostname or ""


def summarize_records(records: Iterable[FetchRecord]) -> dict[str, Any]:
    recs = list(records)
    ok = [r for r in recs if r.ok]
    return {
        "total": len(recs),
        "ok": len(ok),
        "failed": len(recs) - len(ok),
        "bytes": sum(r.bytes for r in recs),
        "elapsed_ms": sum(r.elapsed_ms for r in recs),
        "hosts": sorted({r.host for r in recs if r.host}),
    }
