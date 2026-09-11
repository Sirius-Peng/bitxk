"""统一身份认证（SSO / CAS）与选课系统登录。

登录链路（北京理工大学，2026 年现状）
------------------------------------

::

    GET  https://sso.bit.edu.cn/cas/login?service=<选课系统 casLogin>
      └─ 页面里藏着一组 <p id="...">：
         login-croypto       -> base64 字符串，作为 AES key
         login-page-flowkey  -> 作为 execution
         current-login-type  -> 'UsernamePassword'
    POST 同一个 URL（application/x-www-form-urlencoded）
         username / password(密文) / execution / croypto /
         captcha_code / _eventId=submit / type=UsernamePassword
      └─ 302 回跳 -> https://xk.bit.edu.cn/xsxkapp/.../bitXsxkLogin/casLogin.do?bitXsxkLogin=<key>
    GET  .../student/register.do?number=<key>
      └─ {"data": {"token": "...", "name": "..."}}   <- 之后所有请求带 header Token

密码加密存在两代实现，本模块都支持：

* ``ecb``（默认，当前 BIT 在用）：``AES-ECB``，key = base64decode(croypto)，
  PKCS#7 填充，密文再 base64。
* ``cbc``（旧版 wisedu，部分学校仍在用）：``AES-128-CBC``，
  明文 = 64 位随机串 + 密码，key = salt 的前 16 字节，iv = 16 位随机串，密文 base64。

另外支持**手动导入**浏览器 Cookie/Token（``from_manual``），这样即使
SSO 改版导致自动登录失效，工具依然可用 —— 这是刻意设计的兜底路径。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import CaptchaRequired, LoginError, NetworkError, TokenExpired
from .http import HttpClient

logger = logging.getLogger(__name__)

__all__ = ["Credentials", "Session", "BitAuth", "encrypt_password"]

# --------------------------------------------------------------------------
# 站点常量
# --------------------------------------------------------------------------

SSO_BASE = "https://sso.bit.edu.cn"
CAS_LOGIN_URL = f"{SSO_BASE}/cas/login"

XK_BASE = "https://xk.bit.edu.cn/xsxkapp"
"""选课系统基址。"""

CAS_SERVICE_URL = f"{XK_BASE}/sys/xsxkapp/bitXsxkLogin/casLogin.do"
"""CAS 回跳目标 —— 也就是 ``?service=`` 的值。"""

API_BASE = f"{XK_BASE}/sys/xsxkapp"
"""业务接口基址。"""

#: 校外 WebVPN 前缀（走校园 VPN 时可作为备选基址）。
WEBVPN_PREFIX = (
    "https://webvpn.bit.edu.cn/https/"
    "77726476706e69737468656265737421e3e44ed225397c1e7b0c9ce29b5b"
)

_ALPHABET = string.ascii_letters + string.digits


# --------------------------------------------------------------------------
# 密码加密
# --------------------------------------------------------------------------

def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def encrypt_password(password: str, key_material: str, mode: str = "ecb") -> str:
    """按 BIT/Wisedu 的约定加密密码。

    Args:
        password: 明文密码。
        key_material: 来自登录页的 ``login-croypto``（base64 字符串）。
        mode: ``"ecb"``（当前 BIT）或 ``"cbc"``（旧版 wisedu）。

    Returns:
        base64 编码的密文。

    Raises:
        LoginError: 依赖缺失或 key 长度非法。
    """
    try:
        from Crypto.Cipher import AES  # type: ignore
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于环境问题
        raise LoginError(
            "缺少 pycryptodome 依赖，无法加密密码。请先安装：pip install pycryptodome"
        ) from exc

    key_material = (key_material or "").strip()
    if not key_material:
        raise LoginError("登录页未返回加密密钥（login-croypto 为空），无法加密密码")

    # croypto 通常是 base64 的 16 字节；若不是合法 base64 就直接当原始 key 用。
    raw_key = None
    try:
        candidate = base64.b64decode(key_material, validate=True)
    except Exception:
        candidate = None
    if candidate is not None and len(candidate) in (16, 24, 32):
        raw_key = candidate
    else:
        # 不是合法 base64，或解出来的长度不是合法 AES 密钥长度，
        # 就把原始字符串当 key（部分站点直接给 16 个可见字符）。
        raw_key = key_material.encode("utf-8")

    mode = (mode or "ecb").lower()

    if mode == "cbc":
        key = raw_key[:16].ljust(16, b"\0") if len(raw_key) >= 16 else raw_key.ljust(16, b"\0")
        iv = "".join(secrets.choice(_ALPHABET) for _ in range(16)).encode("utf-8")
        prefix = "".join(secrets.choice(_ALPHABET) for _ in range(64))
        plain = (prefix + password).encode("utf-8")
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return base64.b64encode(cipher.encrypt(_pkcs7_pad(plain))).decode("ascii")

    # 默认 ECB
    if len(raw_key) not in (16, 24, 32):
        # 长度不对说明页面结构变了，明确报错而不是静默产生错误密文
        raise LoginError(
            f"加密密钥长度非法（{len(raw_key)} 字节），登录页结构可能已变更"
        )
    cipher = AES.new(raw_key, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(_pkcs7_pad(password.encode("utf-8")))).decode("ascii")


# --------------------------------------------------------------------------
# 凭据与会话
# --------------------------------------------------------------------------

@dataclass
class Credentials:
    """登录凭据。"""

    username: str = ""
    password: str = ""

    def is_complete(self) -> bool:
        return bool(self.username and self.password)


@dataclass
class Session:
    """一次可用的选课系统登录态。可序列化到磁盘以便复用。"""

    token: str
    cookies: dict[str, str] = field(default_factory=dict)
    student_name: str = ""
    student_code: str = ""
    created_at: float = field(default_factory=time.time)
    #: 会话来源：``sso`` 自动登录 / ``manual`` 手动导入 / ``cache`` 本地缓存
    origin: str = "sso"

    @property
    def age(self) -> float:
        """会话已存在多少秒。"""
        return time.time() - self.created_at

    def is_probably_fresh(self, max_age: float = 15 * 60) -> bool:
        """是否大概率还有效（选课系统 token 约 15~20 分钟失效）。"""
        return self.age < max_age

    # ------------------------------------------------------------ 持久化

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "cookies": self.cookies,
            "student_name": self.student_name,
            "student_code": self.student_code,
            "created_at": self.created_at,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            token=str(data.get("token", "")),
            cookies=dict(data.get("cookies") or {}),
            student_name=str(data.get("student_name", "")),
            student_code=str(data.get("student_code", "")),
            created_at=float(data.get("created_at", 0) or 0),
            origin=str(data.get("origin", "cache")),
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 会话文件含登录凭据，收紧权限
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - 某些文件系统不支持
            pass

    @classmethod
    def load(cls, path: str | Path) -> "Session | None":
        path = Path(path)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("会话文件损坏，忽略：%s", exc)
            return None
        session = cls.from_dict(data)
        session.origin = "cache"
        return session if session.token else None


# --------------------------------------------------------------------------
# 登录实现
# --------------------------------------------------------------------------

def _parse_tag_value(html: str, element_id: str) -> str | None:
    """从 ``<p id="xxx">value</p>`` 这类标签里取文本。

    页面是 SPA 外壳，这些值是服务端渲染进 HTML 的，用正则即可，
    不引入 BeautifulSoup 依赖。
    """
    pattern = rf'id=["\']{re.escape(element_id)}["\'][^>]*>(.*?)</'
    match = re.search(pattern, html, re.S)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


class BitAuth:
    """负责把「学号+密码」换成可用的 :class:`Session`。"""

    def __init__(
        self,
        http: HttpClient,
        *,
        encrypt_mode: str = "ecb",
        api_base: str = API_BASE,
        cas_login_url: str = CAS_LOGIN_URL,
        service_url: str = CAS_SERVICE_URL,
    ) -> None:
        self.http = http
        self.encrypt_mode = encrypt_mode
        self.api_base = api_base.rstrip("/")
        self.cas_login_url = cas_login_url
        self.service_url = service_url
        self._register_cache_key: str | None = None

    # ------------------------------------------------------------ 自动登录

    def _fetch_login_page(self) -> tuple[str, str]:
        """GET 登录页，返回 ``(croypto, flowkey)``。"""
        url = f"{self.cas_login_url}?service={_quote(self.service_url)}"
        resp = self.http.get(url, allow_redirects=True)
        if resp.status_code >= 400:
            raise LoginError(f"无法访问统一身份认证页面（HTTP {resp.status_code}）")

        html = resp.text
        croypto = _parse_tag_value(html, "login-croypto")
        flowkey = _parse_tag_value(html, "login-page-flowkey")

        if not croypto:
            # 可能是旧版 CAS 页面：从 pwdEncryptSalt 取盐。
            # 注意旧页面用 id 承载该字段，个别版本用 name，两种都试。
            match = re.search(
                r'(?:id|name)=["\']pwdEncryptSalt["\'][^>]*value=["\']([^"\']+)', html
            )
            if match:
                croypto = match.group(1)
                self.encrypt_mode = "cbc"
        if not flowkey:
            match = re.search(
                r'(?:id|name)=["\']execution["\'][^>]*value=["\']([^"\']+)', html
            )
            flowkey = match.group(1) if match else None

        if not croypto or not flowkey:
            raise LoginError(
                "登录页结构已变更：未找到 login-croypto / login-page-flowkey。"
                "请改用 --cookie 手动导入模式，或提交 issue 更新适配。"
            )
        return croypto, flowkey

    # ------------------------------------------------------- 回跳凭据解析

    #: 回跳 URL / Location 里可能承载凭据的参数名。
    #: ``ticket`` 是 CAS 标准形态；``bitXsxkLogin`` 是选课系统自己的换票参数；
    #: ``number`` 是 ``register.do`` 的入参形态。
    _KEY_PATTERNS = (
        r"[?&]bitXsxkLogin=([^&#\s]+)",
        r"[?&]ticket=([^&#\s]+)",
        r"[?&]number=([^&#\s]+)",
    )

    @classmethod
    def _key_from_text(cls, text: str) -> str | None:
        """从一段 URL 文本里提取回跳凭据。"""
        if not text:
            return None
        for pattern in cls._KEY_PATTERNS:
            match = re.search(pattern, text)
            if match:
                return _unquote(match.group(1))
        return None

    def _follow_for_key(self, location: str, max_hops: int = 3) -> str | None:
        """手工跟随后续跳转，直到某一跳的 URL 或响应里出现回跳凭据。

        必须手工跟随：自动跟随会把每一跳的中间响应丢掉，而凭据恰恰藏在
        其中某一跳的 query 里。同时每次只跟一跳，避免意外把登录表单再 POST 一次。
        """
        url = location
        for _ in range(max_hops):
            if not url:
                return None

            # 有的实现直接把凭据放在这一跳的 URL 上
            key = self._key_from_text(url)
            if key:
                return key

            try:
                resp = self.http.get(url, allow_redirects=False)
            except Exception as exc:  # 网络问题不该让整个登录白跑
                logger.debug("跟随后续跳转失败：%s", exc)
                return None

            key = self._key_from_text(resp.headers.get("Location", "")) or self._key_from_text(
                str(getattr(resp, "url", "") or "")
            )
            if key:
                return key

            # 响应正文里也可能带（例如返回一段跳转脚本或空页面）
            if resp.text:
                key = self._key_from_text(resp.text)
                if key:
                    return key

            url = resp.headers.get("Location", "")
        return None

    def login(self, username: str, password: str) -> Session:
        """执行完整登录流程。

        Raises:
            LoginError: 账号密码错误或页面结构变更。
            CaptchaRequired: 需要验证码。
            NetworkError: 网络故障。
        """
        if not username or not password:
            raise LoginError("学号或密码为空")

        logger.debug("获取登录页参数…")
        croypto, flowkey = self._fetch_login_page()

        try:
            encrypted = encrypt_password(password, croypto, self.encrypt_mode)
        except LoginError:
            raise
        except Exception as exc:  # pragma: no cover - 兜底
            raise LoginError(f"密码加密失败：{exc}") from exc

        form = {
            "username": username,
            "password": encrypted,
            "execution": flowkey,
            # 注意是**小写 c** 的 captcha_payload。写成 Captcha_payload 会被
            # 服务端直接忽略（该名字在生产前端 bundle 里出现 0 次）。
            "captcha_payload": "",
            "captcha_code": "",
            "_eventId": "submit",
            "type": "UsernamePassword",
            "geolocation": "",
            "croypto": croypto,
            # riskSystemSwitch 为 USTC 时前端会追加这几个风控字段
            "risk_payload": "",
            "targetSystem": "sso",
            "siteId": "sourceId",
            "riskEngine": "true",
        }

        url = f"{self.cas_login_url}?service={_quote(self.service_url)}"
        logger.debug("提交登录表单…")
        resp = self.http.post(
            url,
            data=form,
            # 关键：**不要自动跟随重定向**。
            # 登录成功返回的是 302 + Location 里的 ?ticket=ST-xxx，
            # 自动跟随会丢掉这个响应头，也就拿不到 ticket。
            allow_redirects=False,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": url,
                "Origin": SSO_BASE,
            },
        )

        # 兼容：多数情况返回 302；若服务端直接 200 吐了页面，也尝试从正文找凭据
        key: str | None = None
        location = ""
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            key = self._key_from_text(location)
            if not key:
                # Location 指向的 service 地址里没有凭据，继续手工跟一跳
                key = self._follow_for_key(location)
        else:
            self._raise_login_error(resp)
            key = self._key_from_text(resp.url) or self._key_from_text(resp.text)

        if not key:
            raise LoginError(
                "登录未取得回跳凭据：可能是账号密码错误，或 SSO 页面结构变更。"
            )

        return self._register(username, key, location)

    def _raise_login_error(self, resp) -> None:
        """把登录失败响应翻译成精确异常。"""
        text = resp.text or ""
        code = _extract_error_element(text, "login-error-msg")

        if code == "1320007":
            # 该错误码有两种成因：本会话需要验证码，或该会话的登录请求已失效
            #（实测：同一 session 第 2 次 POST 必定得到它）。
            # 两种情况的处理方式相同 —— 丢弃当前会话重新来一次。
            raise CaptchaRequired(
                "统一身份认证要求验证码或本次会话已失效（错误码 1320007）。"
                "请先在浏览器里成功登录一次，再运行本工具；"
                "或使用 --cookie/--token 手动导入登录态。"
            )

        # 账号锁定等错误码（来自前端 i18n 表）
        if code in ("1030028",):
            raise LoginError("账号已被锁定，请联系学校信息化部门解锁")

        tip = _extract_error_tip(text) or code
        if resp.status_code in (401, 403):
            raise LoginError(f"统一身份认证失败：{tip or '账号或密码错误'}")
        if resp.status_code >= 500:
            raise LoginError(f"统一身份认证服务异常（HTTP {resp.status_code}），请稍后再试")
        raise LoginError(f"统一身份认证失败：{tip or '未知原因'}")

    # ------------------------------------------------------------ 注册换票

    def _register(self, username: str, ticket: str, location: str = "") -> Session:
        """完成选课侧的会话建立并换取 Token。

        有两种可能的落点，都试一遍：

        1. ``?ticket=ST-xxx`` 的 CAS ticket —— 需要回打一次 service 地址
           （也就是选课系统的 ``bitXsxkLogin/casLogin.do``），由它完成换票；
        2. ``bitXsxkLogin=<key>`` —— 这已经是选课系统的会话凭据，
           直接拿去调 ``register.do``。
        """
        key = ticket

        if location and not location.startswith("http"):
            location = f"{XK_BASE}/{location.lstrip('/')}"

        if location and "ticket=" in location and ticket.startswith("ST-"):
            # CAS 标准路径：回打 service 完成换票，从跳转链里取出 xk 侧凭据
            try:
                resp = self.http.get(location, allow_redirects=False)
            except Exception as exc:
                logger.debug("回打 service 失败：%s", exc)
                resp = None

            if resp is not None:
                key = (
                    self._key_from_text(resp.headers.get("Location", ""))
                    or self._key_from_text(str(getattr(resp, "url", "") or ""))
                    or self._key_from_text(resp.text or "")
                    or self._follow_for_key(resp.headers.get("Location", ""))
                    or ticket
                )

        url = f"{self.api_base}/student/register.do"
        resp = self.http.get(url, params={"number": key})

        if resp.status_code in (401, 403):
            raise LoginError("换取选课系统 token 失败：登录态未被选课系统接受（HTTP %d）"
                             % resp.status_code)

        try:
            payload = resp.json()
        except Exception as exc:
            snippet = (resp.text or "")[:160].replace("\n", " ")
            raise LoginError(
                f"换取 token 失败：返回的不是 JSON（{snippet}）。"
                "这通常意味着 CAS 换票链路与当前适配不符，"
                "请改用 --cookie/--token 手动导入登录态。"
            ) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("token"):
            code = payload.get("code") if isinstance(payload, dict) else None
            msg = payload.get("msg") if isinstance(payload, dict) else str(payload)[:120]
            raise LoginError(f"换取 token 失败：code={code} msg={msg}")

        return Session(
            token=str(data["token"]),
            cookies=dict(self.http.cookies),
            student_name=str(data.get("name", "") or ""),
            student_code=str(data.get("number", "") or username),
            origin="sso",
        )

    # ------------------------------------------------------------ 手动导入

    @staticmethod
    def from_manual(
        token: str = "",
        cookie: str = "",
        *,
        student_code: str = "",
        student_name: str = "",
    ) -> Session:
        """从浏览器复制出来的 Token / Cookie 构造会话。

        Args:
            token: 选课系统接口请求头里的 ``Token`` 值（也可直接粘贴带
                ``bitXsxkLogin=`` 的完整回跳 URL，本方法会自动提取）。
            cookie: 浏览器 Network 面板里复制的一整行 ``Cookie``。
            student_code: 学号（可选，便于展示）。
        """
        token = (token or "").strip()
        if "bitXsxkLogin=" in token:
            match = re.search(r"bitXsxkLogin=([^&#\s]+)", token)
            if match:
                token = _unquote(match.group(1))
        if token.lower().startswith("token:"):
            token = token.split(":", 1)[1].strip()

        cookies: dict[str, str] = {}
        for part in (cookie or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            cookies[name.strip()] = value.strip()

        return Session(
            token=token,
            cookies=cookies,
            student_name=student_name,
            student_code=student_code,
            origin="manual",
        )


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _unquote(text: str) -> str:
    from urllib.parse import unquote

    return unquote(text)


def _extract_error_element(html: str, element_id: str) -> str | None:
    """提取指定 id 元素的文本（会剥掉内部标签）。

    登录失败页把错误码放在 ``#login-error-msg`` 里的一个 ``<span>`` 中，
    所以不能直接用 ``_parse_tag_value``（那会遇到嵌套标签就截断）。
    """
    pattern = rf'id=["\']{re.escape(element_id)}["\'][^>]*>(.*?)</'
    match = re.search(pattern, html, re.S)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", " ", match.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _extract_error_tip(html: str) -> str | None:
    """从登录失败页面里扒出人话错误提示。"""
    for element_id in ("login-error-msg", "login-error-code", "caLoginFailTip", "msg"):
        text = _extract_error_element(html, element_id)
        if text:
            return text
    match = re.search(
        r'<div[^>]*class=["\'][^"\']*error[^"\']*["\'][^>]*>(.*?)</div>', html, re.S
    )
    if match:
        text = re.sub(r"<[^>]+>", " ", match.group(1)).strip()
        if text:
            return text
    return None
