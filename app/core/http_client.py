"""安全 HTTP 客户端：限流、重试、SSRF 防护、体积限制、获取留痕。

设计约束（对应开发方案 §9 安全）：
- 禁止下载器访问内网地址（默认关闭私有地址访问）；
- 限制文件大小与解析时间；
- 每个请求记录来源、耗时、状态，供数据源健康监控使用。
"""

from __future__ import annotations

import ipaddress
import random
import queue
import uuid
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
import httpcore
from contextlib import contextmanager

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
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)


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

    def acquire(self, host: str, deadline: float | None = None) -> None:
        while True:
            with self._lock:
                bucket = self._buckets.setdefault(host, _Bucket(self.rps))
                now = time.monotonic()
                wait = bucket.last + (1.0 / self.rps) - now
                if wait <= 0:
                    bucket.last = now
                    return
            if deadline is not None and time.time() + wait >= deadline:
                raise FetchError("任务期限已到，停止等待限流", stage="deadline")
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


def assert_safe_url(url: str, *, timeout: float | None = None) -> str:
    """校验 URL 合法性，默认阻断内网与非法协议。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError(f"非安全协议: {parsed.scheme}", url=url, stage="guard")
    host = parsed.hostname
    if not host:
        raise FetchError("缺少主机名", url=url, stage="guard")
    if parsed.username or parsed.password:
        raise FetchError("URL 不允许包含凭证", url=url, stage="guard")
    _public_addresses(host, timeout)
    return url


_dns_slots = threading.BoundedSemaphore(8)


def _resolve_host(host: str, timeout: float | None) -> list[str]:
    # 系统 DNS 解析没有 socket timeout，限制等待时间和挂起解析数量，避免占住扫描线程。
    timeout = settings.http_timeout if timeout is None else timeout
    started = time.monotonic()
    if timeout <= 0 or not _dns_slots.acquire(timeout=timeout):
        raise FetchError("DNS 解析等待超时", stage="guard")
    result = queue.Queue(maxsize=1)
    def resolve():
        try:
            result.put(socket.getaddrinfo(host, None))
        except Exception as exc:
            result.put(exc)
        finally:
            _dns_slots.release()
    threading.Thread(target=resolve, daemon=True).start()
    try:
        value = result.get(timeout=max(0, timeout - (time.monotonic() - started)))
    except queue.Empty:
        raise FetchError("DNS 解析超时", stage="guard")
    if isinstance(value, Exception):
        raise FetchError(f"域名解析失败: {host}", stage="guard") from value
    return list(dict.fromkeys(info[4][0] for info in value))


def _public_addresses(host: str, timeout: float | None = None) -> list[str]:
    try:
        literal = ipaddress.ip_address(host.split("%")[0])
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = _resolve_host(host, timeout)
        except socket.gaierror as exc:
            raise FetchError(f"域名解析失败: {host}", stage="guard") from exc
    if not addresses:
        raise FetchError("域名未返回地址", stage="guard")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if not settings.allow_private_address and (not ip.is_global or ip.is_multicast or ip.is_reserved):
            raise FetchError(f"拒绝访问非公网地址: {host}", stage="guard")
    return addresses


class PublicNetworkBackend(httpcore.SyncBackend):
    """在真正建立连接时重新验证 DNS，并连接已验证的 IP，TLS 仍验证原域名。"""
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if not settings.enable_network:
            raise FetchError("已关闭网络访问", stage="guard")
        started = time.monotonic()
        addresses = _public_addresses(host, timeout)
        last = None
        for address in addresses:
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout()
            try:
                return super().connect_tcp(address, port, remaining, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last or httpcore.ConnectError()


def public_transport() -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport(trust_env=False)
    # HTTPX does not expose a backend argument; keep this single integration point tested.
    transport._pool._network_backend = PublicNetworkBackend()
    return transport


class HttpClient:
    """带限流、重试和留痕的 HTTP 客户端。"""

    def __init__(self, records: list[FetchRecord] | None = None):
        self.records: list[FetchRecord] = records if records is not None else []
        self.deadline: float | None = None
        self._client = httpx.Client(
            timeout=settings.http_timeout,
            follow_redirects=False,
            transport=public_transport(),
            trust_env=False,
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

    def _remaining(self, timeout: float) -> float:
        if not settings.enable_network:
            raise FetchError("已关闭网络访问（ENABLE_NETWORK=false）", stage="guard")
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.time())
        if timeout <= 0:
            raise FetchError("任务期限已到", stage="deadline")
        return timeout

    def _sleep(self, seconds: float) -> None:
        if self.deadline is not None and time.time() + seconds >= self.deadline:
            raise FetchError("任务期限已到，停止重试", stage="deadline")
        time.sleep(seconds)

    @contextmanager
    def _stream(self, method: str, url: str, *, timeout: float, **kwargs):
        request = self._client.build_request(method, url, timeout=self._remaining(timeout), **kwargs)
        for _ in range(11):
            self._remaining(timeout)
            assert_safe_url(str(request.url), timeout=self._remaining(timeout))
            limiter.acquire(request.url.host, self.deadline)
            remaining = self._remaining(timeout)
            request.extensions["timeout"] = dict(connect=remaining, read=remaining, write=remaining, pool=remaining)
            response = self._client.send(request, stream=True, follow_redirects=False)
            if response.has_redirect_location:
                next_request = response.next_request
                response.close()
                if next_request is None:
                    raise FetchError("重定向缺少目标", url=url, stage="guard")
                request = next_request
                continue
            try:
                yield response
            finally:
                response.close()
            return
        raise FetchError("重定向次数超过上限", url=url, stage="guard")

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
        self._remaining(timeout or settings.http_timeout)
        assert_safe_url(url, timeout=self._remaining(timeout or settings.http_timeout))
        host = urlparse(url).hostname or ""
        attempts = 0
        max_attempts = max(1, retries if retries is not None else settings.http_retries)
        last_error: Exception | None = None

        while attempts < max_attempts:
            attempts += 1
            started = time.monotonic()
            try:
                with self._stream(method, url, params=params, data=data, json=json_body,
                                  headers=headers, timeout=timeout or settings.http_timeout) as resp:
                    body = bytearray()
                    for chunk in resp.iter_bytes(65536):
                        self._remaining(timeout or settings.http_timeout)
                        body.extend(chunk)
                        if len(body) > settings.max_file_bytes:
                            raise FetchError("响应超过体积上限", url=url, stage=stage)
                    resp._content = bytes(body)
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
                        self._sleep(settings.http_backoff * attempts)
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
                    self._sleep(settings.http_backoff * attempts)
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

        self._remaining(settings.download_timeout)
        assert_safe_url(url, timeout=self._remaining(settings.download_timeout))
        dest = Path(dest)
        if not dest.parent.exists():
            dest.parent.mkdir(parents=True)
        host = urlparse(url).hostname or ""
        headers = dict(headers_extra := ({"Referer": referer} if referer else {}))
        started = time.monotonic()
        written = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with self._stream(
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
                        self._remaining(settings.download_timeout)
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
