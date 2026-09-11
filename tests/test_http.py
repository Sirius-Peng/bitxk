"""HTTP 层测试：限速、重试、限流识别。

不依赖真实网络。
"""

from __future__ import annotations

import threading
import time

import pytest
import requests

from bitxk.exceptions import NetworkError, RateLimited
from bitxk.http import HttpClient, RateLimiter


class TestRateLimiter:
    def test_首次请求不等待(self):
        limiter = RateLimiter(min_interval=5.0, jitter=0.0)
        start = time.monotonic()
        limiter.acquire()
        assert time.monotonic() - start < 0.1

    def test_第二次请求被推迟(self):
        limiter = RateLimiter(min_interval=0.25, jitter=0.0)
        limiter.acquire()
        start = time.monotonic()
        limiter.acquire()
        assert time.monotonic() - start >= 0.2

    def test_惩罚会推后闸门(self):
        limiter = RateLimiter(min_interval=0.1, jitter=0.0)
        limiter.acquire()
        limiter.penalize(0.3)
        start = time.monotonic()
        limiter.acquire()
        assert time.monotonic() - start >= 0.25

    def test_多线程下间隔仍被保证(self):
        """并发轮询多门课时，全局限速必须生效，否则会把服务器打挂。"""
        limiter = RateLimiter(min_interval=0.05, jitter=0.0)
        stamps: list[float] = []
        lock = threading.Lock()

        def worker():
            limiter.acquire()
            with lock:
                stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert all(gap >= 0.04 for gap in gaps), f"间隔不足：{gaps}"


class TestHttpClient:
    def test_设置与清除_token(self):
        client = HttpClient(min_interval=0)
        client.set_token("abc")
        assert client.session.headers["Token"] == "abc"
        client.set_token(None)
        assert "Token" not in client.session.headers

    def test_默认_ua_已设置(self):
        client = HttpClient(min_interval=0)
        assert "Mozilla" in client.session.headers["User-Agent"]

    def test_自定义_header_覆盖默认(self):
        client = HttpClient(min_interval=0, base_headers={"User-Agent": "custom/1.0"})
        assert client.session.headers["User-Agent"] == "custom/1.0"

    def test_cookie_往返(self):
        client = HttpClient(min_interval=0)
        client.cookies = {"JSESSIONID": "xyz"}
        assert client.cookies == {"JSESSIONID": "xyz"}

    def test_替换_cookie_会清空旧的(self):
        client = HttpClient(min_interval=0)
        client.cookies = {"A": "1"}
        client.cookies = {"B": "2"}
        assert client.cookies == {"B": "2"}

    def test_代理被配置(self):
        client = HttpClient(min_interval=0, proxy="http://127.0.0.1:8080")
        assert client.session.proxies["https"] == "http://127.0.0.1:8080"

    def test_超时重试后抛出_network_error(self, monkeypatch):
        client = HttpClient(min_interval=0, max_retries=1)

        def always_timeout(*a, **k):
            raise requests.Timeout("超时")

        monkeypatch.setattr(client.session, "request", always_timeout)
        with pytest.raises(NetworkError, match="仍失败"):
            client.get("https://example.invalid")

    def test_429_被识别为限流(self, monkeypatch):
        client = HttpClient(min_interval=0, max_retries=0)

        class Resp:
            status_code = 429
            text = ""

        monkeypatch.setattr(client.session, "request", lambda *a, **k: Resp())
        with pytest.raises(RateLimited):
            client.get("https://example.invalid")

    def test_403_含频繁字样视为限流(self, monkeypatch):
        client = HttpClient(min_interval=0, max_retries=0)

        class Resp:
            status_code = 403
            text = "访问过于频繁，请稍后再试"

        monkeypatch.setattr(client.session, "request", lambda *a, **k: Resp())
        with pytest.raises(RateLimited):
            client.get("https://example.invalid")

    def test_普通_403_不当作限流(self, monkeypatch):
        """并非所有 403 都是限流，无限重试反而更糟。"""
        client = HttpClient(min_interval=0, max_retries=0)

        class Resp:
            status_code = 403
            text = "Forbidden"

        monkeypatch.setattr(client.session, "request", lambda *a, **k: Resp())
        resp = client.get("https://example.invalid")
        assert resp.status_code == 403

    def test_重试后成功(self, monkeypatch):
        client = HttpClient(min_interval=0, max_retries=2)
        attempts = {"n": 0}

        class Resp:
            status_code = 200
            text = "ok"

        def flaky(*a, **k):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise requests.ConnectionError("抖了一下")
            return Resp()

        monkeypatch.setattr(client.session, "request", flaky)
        assert client.get("https://example.invalid").status_code == 200
        assert attempts["n"] == 2

    def test_上下文管理器关闭会话(self):
        with HttpClient(min_interval=0) as client:
            assert client.session is not None
