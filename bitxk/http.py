"""HTTP 传输层：会话、重试、限速、校内/校外 WebVPN 自动探测。

单独抽一层的原因：轮询场景下「第几秒发一次请求」和「失败了怎么办」
是核心策略，把它和业务接口解耦才能分别调优和测试。
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .exceptions import NetworkError, RateLimited

logger = logging.getLogger(__name__)

__all__ = ["HttpClient", "RateLimiter"]

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class RateLimiter:
    """线程安全的令牌式限速器：保证两次请求之间至少间隔 ``min_interval`` 秒。"""

    def __init__(self, min_interval: float = 1.0, jitter: float = 0.3) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.jitter = max(0.0, float(jitter))
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        """阻塞直到可以发起下一次请求。"""
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            delay = self.min_interval + (random.uniform(0, self.jitter) if self.jitter else 0.0)
            self._next_allowed = now + delay

    def penalize(self, seconds: float) -> None:
        """被限流时把闸门往后推，避免火上浇油。"""
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


class HttpClient:
    """对 ``requests.Session`` 的薄封装。

    负责任务：
    * 统一 UA / Header / Cookie / Token；
    * 自动重试网络抖动（指数退避）；
    * 全局限速，避免把学校服务器打挂；
    * 识别「被限流」和「登录失效」这两类需要特殊处理的响应。
    """

    def __init__(
        self,
        *,
        base_headers: dict[str, str] | None = None,
        min_interval: float = 1.0,
        timeout: float = 10.0,
        max_retries: int = 3,
        verify: bool = True,
        proxy: str | None = None,
    ) -> None:
        self.session = requests.Session()
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.verify = verify
        self.limiter = RateLimiter(min_interval)
        self.token: str | None = None

        headers = {"User-Agent": DEFAULT_UA, "Accept": "application/json, text/plain, */*"}
        if base_headers:
            headers.update(base_headers)
        self.session.headers.update(headers)

        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        # 连接池放大一点，轮询时复用 TCP 连接更省事
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------ 属性

    @property
    def cookies(self) -> dict[str, str]:
        return self.session.cookies.get_dict()

    @cookies.setter
    def cookies(self, value: dict[str, str]) -> None:
        self.session.cookies.clear()
        for key, val in (value or {}).items():
            self.session.cookies.set(key, val)

    def set_token(self, token: str | None) -> None:
        self.token = token
        if token:
            self.session.headers["Token"] = token
        else:
            self.session.headers.pop("Token", None)

    # ------------------------------------------------------------ 请求

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """发一个请求，带限速与重试。"""
        headers = dict(kwargs.pop("headers", None) or {})
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                resp = self.session.request(method, url, headers=headers or None, **kwargs)
            except requests.Timeout as exc:
                last_error = exc
                self._backoff(attempt, f"请求超时（第 {attempt + 1} 次）")
                continue
            except requests.ConnectionError as exc:
                last_error = exc
                self._backoff(attempt, f"连接失败（第 {attempt + 1} 次）")
                continue
            except requests.RequestException as exc:
                raise NetworkError(f"请求失败：{exc}") from exc

            if self._is_rate_limited(resp):
                self.limiter.penalize(5.0)
                raise RateLimited(f"服务端限流（HTTP {resp.status_code}）")

            return resp

        raise NetworkError(f"网络请求在 {self.max_retries + 1} 次尝试后仍失败：{last_error}")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    # ------------------------------------------------------------ 内部

    def _backoff(self, attempt: int, reason: str) -> None:
        if attempt >= self.max_retries:
            logger.debug("%s —— 已达最大重试次数", reason)
            return
        delay = min(2**attempt + random.uniform(0, 0.5), 15.0)
        logger.debug("%s，%.1fs 后重试", reason, delay)
        time.sleep(delay)

    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        if resp.status_code == 429:
            return True
        if resp.status_code == 403:
            text = (resp.text or "")[:2000].lower()
            return "频繁" in text or "rate" in text or "captcha" in text
        return False

    def close(self) -> None:
        with contextlib.suppress(Exception):  # 关闭失败无需上报
            self.session.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
