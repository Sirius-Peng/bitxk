"""登录契约回归测试。

这里的每个用例都对应一个**实测踩过的坑**，改动 auth.py 时务必保持通过。
契约细节来自对生产前端 bundle 与真实 SSO 服务的逆向分析。
"""

from __future__ import annotations

import pytest

from bitxk.auth import BitAuth, _extract_error_element, _extract_error_tip
from bitxk.exceptions import CaptchaRequired, LoginError


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url

    def json(self):
        """像真实响应一样解析正文；不是 JSON 就抛 ValueError。"""
        import json as _json

        if not self.text:
            raise ValueError("empty body")
        return _json.loads(self.text)


class RecordingHttp:
    """记录所有请求的假 HTTP 层。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.cookies = {}

    def _next(self):
        if not self.responses:
            raise AssertionError("没有预置响应了")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._next()

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._next()

    def set_token(self, token):
        pass


LOGIN_PAGE = """
<html><body>
  <p id="current-login-type">UsernamePassword</p>
  <p id="login-croypto">MDEyMzQ1Njc4OWFiY2RlZg==</p>
  <p id="login-page-flowkey">uuid-1_ZXlKaGJHY2lPaUpJVXpVeE1pSXNJblI1Y0NJNklrcFhWQ0o5</p>
</body></html>
"""


def make_auth(*responses):
    http = RecordingHttp(list(responses))
    return BitAuth(http), http


class TestLoginFormContract:
    def test_密码字段名必须是小写captcha_payload(self):
        """实测：前端 bundle 里只有 captcha_payload；
        老实现写的 Captcha_payload（大写 C）会被服务端静默忽略。"""
        auth, http = make_auth(
            FakeResponse(200, LOGIN_PAGE),  # GET 登录页
            FakeResponse(302, headers={"Location": "x"}),  # POST 结果
        )
        with pytest.raises(LoginError):
            auth.login("u", "p")
        form = http.calls[1]["data"]
        assert "captcha_payload" in form
        assert "Captcha_payload" not in form

    def test_必需字段齐全(self):
        auth, http = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(302, headers={"Location": "x"}),
        )
        with pytest.raises(LoginError):
            auth.login("u", "p")
        form = http.calls[1]["data"]
        for field in ("username", "password", "execution", "croypto", "type", "_eventId"):
            assert field in form, f"缺少必需字段 {field}"
        assert form["_eventId"] == "submit"
        assert form["type"] == "UsernamePassword"

    def test_execution_与_croypto_原样回填(self):
        auth, http = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(302, headers={"Location": "x"}),
        )
        with pytest.raises(LoginError):
            auth.login("u", "p")
        form = http.calls[1]["data"]
        assert form["croypto"] == "MDEyMzQ1Njc4OWFiY2RlZg=="
        assert form["execution"].startswith("uuid-1_")

    def test_提交时禁止自动跟随重定向(self):
        """ticket 藏在 302 的 Location 里，自动跟随会把它丢掉。"""
        auth, http = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(302, headers={"Location": "x"}),
        )
        with pytest.raises(LoginError):
            auth.login("u", "p")
        assert http.calls[1]["allow_redirects"] is False

    def test_表单以_urlencoded_提交(self):
        auth, http = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(302, headers={"Location": "x"}),
        )
        with pytest.raises(LoginError):
            auth.login("u", "p")
        assert http.calls[1]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


class TestLoginFailures:
    def test_401_报账号密码错误(self):
        auth, _ = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(401, '<p id="login-error-msg"><span>用户名或密码错误</span></p>'),
        )
        with pytest.raises(LoginError, match="用户名或密码错误"):
            auth.login("u", "wrong")

    def test_错误码1320007_提示验证码或会话失效(self):
        """实测：同一 session 第二次 POST 必定得到 1320007。

        两种成因（需验证码 / 会话一次性失效）处理方式相同 —— 都要重建会话。
        """
        auth, _ = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(200, '<p id="login-error-msg"><span>1320007</span></p>'),
        )
        with pytest.raises(CaptchaRequired, match="1320007"):
            auth.login("u", "p")

    def test_账号锁定有专门提示(self):
        auth, _ = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(200, '<p id="login-error-msg"><span>1030028</span></p>'),
        )
        with pytest.raises(LoginError, match="锁定"):
            auth.login("u", "p")

    def test_500_空body_报服务异常(self):
        """实测：漏传 type 字段会导致 500 + 空 body。"""
        auth, _ = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(500, ""),
        )
        with pytest.raises(LoginError, match="服务异常"):
            auth.login("u", "p")

    def test_200_重渲染页面_无错误提示时报未知原因(self):
        """实测：漏 _eventId 或漏 Cookie 时会 200 重渲染且无错误元素。"""
        auth, _ = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(200, LOGIN_PAGE),
        )
        with pytest.raises(LoginError, match="未知原因"):
            auth.login("u", "p")

    def test_service_未注册时会被识别(self):
        auth, _ = make_auth(
            FakeResponse(200, '<p id="canot-access-code">1510051</p>'),
        )
        with pytest.raises(LoginError, match=r"结构已变更|未找到"):
            auth.login("u", "p")


class TestTicketFlow:
    def test_302_的_ticket_会被回打到_service(self):
        """CAS 标准路径：POST 得到 302 ?ticket=ST-xxx，需回打 service 换票。"""
        auth, http = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(
                302,
                headers={
                    "Location": "https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/"
                    "bitXsxkLogin/casLogin.do?ticket=ST-abc-123"
                },
            ),
            # 回打 service 得到带 xk 侧凭据的跳转
            FakeResponse(
                302,
                headers={
                    "Location": "https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/"
                    "student/register.do?bitXsxkLogin=KEY456"
                },
            ),
            # register.do 返回 token
            FakeResponse(200, '{"code":"1","data":{"token":"TOK","name":"张三"}}'),
        )
        session = auth.login("u", "p")
        assert session.token == "TOK"
        assert session.student_name == "张三"
        assert any("ticket=ST-abc-123" in c["url"] for c in http.calls)

    def test_直接拿到_bitXsxkLogin_时直接用(self):
        auth, http = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(
                302,
                headers={
                    "Location": "https://xk.bit.edu.cn/x/xsxkLogin/casLogin.do?bitXsxkLogin=DIRECT"
                },
            ),
            FakeResponse(200, '{"code":"1","data":{"token":"T2","name":"李四"}}'),
        )
        session = auth.login("u", "p")
        assert session.token == "T2"
        # 最后一次请求应该带上 DIRECT
        assert http.calls[-1]["params"]["number"] == "DIRECT"

    def test_中间跳转里出现凭据时手工跟随(self):
        auth, http = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(302, headers={"Location": "https://xk.bit.edu.cn/step1"}),
            FakeResponse(
                302, headers={"Location": "https://xk.bit.edu.cn/final?bitXsxkLogin=FOUND"}
            ),
            FakeResponse(200, '{"code":"1","data":{"token":"T3"}}'),
        )
        session = auth.login("u", "p")
        assert session.token == "T3"
        assert http.calls[-1]["params"]["number"] == "FOUND"

    def test_register_失败时报错并建议手动导入(self):
        auth, _ = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(302, headers={"Location": "https://xk.bit.edu.cn/x?bitXsxkLogin=K"}),
            FakeResponse(200, text="<html>不是 JSON</html>"),
        )
        with pytest.raises(LoginError, match="手动导入"):
            auth.login("u", "p")

    def test_register_返回业务错误时带上code与msg(self):
        auth, _ = make_auth(
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(302, headers={"Location": "https://xk.bit.edu.cn/x?bitXsxkLogin=K"}),
            FakeResponse(200, '{"code":"-1","msg":"会话不存在"}'),
        )
        with pytest.raises(LoginError, match="会话不存在"):
            auth.login("u", "p")


class TestErrorParsing:
    def test_解析带span的错误元素(self):
        html = '<p id="login-error-msg"><span>用户名或密码错误</span></p>'
        assert _extract_error_element(html, "login-error-msg") == "用户名或密码错误"

    def test_错误提示优先取_login_error_msg(self):
        html = '<p id="login-error-code">旧字段</p><p id="login-error-msg"><span>新字段</span></p>'
        assert _extract_error_tip(html) == "新字段"

    def test_兼容旧的_error_code_字段(self):
        html = '<p id="login-error-code">用户名或密码错误</p>'
        assert _extract_error_tip(html) == "用户名或密码错误"

    def test_无错误元素返回_None(self):
        assert _extract_error_tip("<html></html>") is None


class TestKeyExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("https://x/a.do?bitXsxkLogin=K1", "K1"),
            ("https://x/a.do?ticket=ST-1&x=2", "ST-1"),
            ("https://x/a.do?number=N1", "N1"),
            ("/relative/path?bitXsxkLogin=K2", "K2"),
            ("https://x/a.do?a=1&bitXsxkLogin=K3&b=2", "K3"),
            ("https://x/a.do?bitXsxkLogin=K%204", "K 4"),
            ("https://x/a.do", None),
            ("", None),
        ],
    )
    def test_从文本提取凭据(self, text, expected):
        assert BitAuth._key_from_text(text) == expected

    def test_bitXsxkLogin_优先于_ticket(self):
        """两个都在时优先用选课系统自己的凭据，避免多余的换票往返。"""
        text = "https://x/a.do?ticket=ST-1&bitXsxkLogin=K"
        assert BitAuth._key_from_text(text) == "K"
