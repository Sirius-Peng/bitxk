"""bitxk —— 北京理工大学（wisedu xsxkapp）选课助手。

快速开始::

    from bitxk import Config, load_config, BitAuth, HttpClient, XkClient, Poller

    cfg = load_config("config.toml")
    http = HttpClient(min_interval=cfg.poll.min_request_interval)
    auth = BitAuth(http)
    session = auth.login(cfg.username, cfg.password)
    http.cookies = session.cookies
    http.set_token(session.token)

    client = XkClient(http)
    batch = client.current_batch()
    print(batch)

也可以直接用命令行：``bitxk grab``。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .auth import API_BASE, BitAuth, Credentials, Session, encrypt_password
from .client import CourseType, XkClient
from .config import Config, PollConfig, WatchTarget, load_config
from .exceptions import (
    ApiError,
    BitxkError,
    CaptchaRequired,
    ConfigError,
    LoginError,
    NetworkError,
    NotInBatchError,
    RateLimited,
    TokenExpired,
)
from .http import HttpClient
from .models import (
    Batch,
    Course,
    CourseStatus,
    SelectionOutcome,
    SelectionResult,
    TeachingClass,
)
from .poller import Poller, PollStats

__all__ = [
    "__version__",
    # auth
    "BitAuth",
    "Credentials",
    "Session",
    "encrypt_password",
    "API_BASE",
    # client
    "XkClient",
    "CourseType",
    # config
    "Config",
    "PollConfig",
    "WatchTarget",
    "load_config",
    # http
    "HttpClient",
    # models
    "Batch",
    "Course",
    "CourseStatus",
    "TeachingClass",
    "SelectionOutcome",
    "SelectionResult",
    # poller
    "Poller",
    "PollStats",
    # exceptions
    "BitxkError",
    "ApiError",
    "CaptchaRequired",
    "ConfigError",
    "LoginError",
    "NetworkError",
    "NotInBatchError",
    "RateLimited",
    "TokenExpired",
]
