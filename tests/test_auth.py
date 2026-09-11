"""认证模块测试：密码加密与登录链路解析。"""

from __future__ import annotations

import base64

import pytest

from bitxk.auth import BitAuth, Session, _extract_error_tip, _parse_tag_value, encrypt_password
from bitxk.exceptions import LoginError
from bitxk.http import HttpClient


@pytest.fixture
def aes_key() -> str:
    """一个固定的 16 字节 AES key（base64）。"""
    return base64.b64encode(b"0123456789abcdef").decode()


class TestEncryptPassword:
    def test_ecb_加密可被标准实现还原(self, aes_key):
        from Crypto.Cipher import AES

        ciphertext = encrypt_password("mypassword", aes_key, "ecb")
        raw = base64.b64decode(ciphertext)
        assert len(raw) % 16 == 0

        plain = AES.new(b"0123456789abcdef", AES.MODE_ECB).decrypt(raw)
        # PKCS#7：末字节是填充长度
        pad = plain[-1]
        assert plain[:-pad].decode("utf-8") == "mypassword"

    def test_cbc_模式明文含随机前缀(self, aes_key):

        ciphertext = encrypt_password("mypassword", aes_key, "cbc")
        raw = base64.b64decode(ciphertext)
        # CBC 的 iv 随机且未随密文返回，这里断言长度契约：
        # 明文 = 64 随机字符 + 9 字符密码 = 73 字节 -> PKCS#7 填充到 80 字节 -> 5 个块。
        assert len(raw) == 80
        # 再用全零 IV 解首块无意义，故只校验「密文不是 ECB 结果」，确保模式确实不同。
        assert raw != base64.b64decode(encrypt_password("mypassword", aes_key, "ecb"))

    def test_相同明文两次加密结果不同_ecb_除外(self, aes_key):
        """ECB 无 IV，相同明文必然同密文 —— 这是该算法已知特性，测试用于固化契约。"""
        assert encrypt_password("same", aes_key, "ecb") == encrypt_password("same", aes_key, "ecb")

    def test_cbc_两次结果不同(self, aes_key):
        assert encrypt_password("same", aes_key, "cbc") != encrypt_password("same", aes_key, "cbc")

    def test_空密钥报错(self):
        with pytest.raises(LoginError, match="加密密钥"):
            encrypt_password("pw", "")

    def test_密钥长度非法时报错而不是静默出错(self):
        with pytest.raises(LoginError, match="长度非法"):
            encrypt_password("pw", base64.b64encode(b"short").decode(), "ecb")

    def test_非base64密钥按原始字节处理(self):
        # 有些站点直接把 16 个可见字符当 key 用
        result = encrypt_password("pw", "0123456789abcdef", "ecb")
        assert base64.b64decode(result)

    def test_中文密码(self, aes_key):
        from Crypto.Cipher import AES

        ciphertext = encrypt_password("密码123", aes_key, "ecb")
        plain = AES.new(b"0123456789abcdef", AES.MODE_ECB).decrypt(base64.b64decode(ciphertext))
        pad = plain[-1]
        assert plain[:-pad].decode("utf-8") == "密码123"


class TestParseTagValue:
    def test_解析_p_标签(self):
        html = '<p id="login-croypto">ABC==</p>'
        assert _parse_tag_value(html, "login-croypto") == "ABC=="

    def test_解析多行与空白(self):
        html = '<p id="login-page-flowkey">\n  XYZ\n</p>'
        assert _parse_tag_value(html, "login-page-flowkey") == "XYZ"

    def test_元素不存在返回_None(self):
        assert _parse_tag_value("<html></html>", "login-croypto") is None

    def test_空内容返回_None(self):
        assert _parse_tag_value('<p id="login-croypto"></p>', "login-croypto") is None

    def test_属性顺序不影响解析(self):
        html = '<p class="x" id="login-croypto">V</p>'
        assert _parse_tag_value(html, "login-croypto") == "V"


class TestExtractErrorTip:
    def test_提取登录错误码(self):
        html = '<p id="login-error-code">用户名或密码错误</p>'
        assert _extract_error_tip(html) == "用户名或密码错误"

    def test_去除内嵌_html_标签(self):
        html = '<p id="caLoginFailTip"><span>账号被锁定</span></p>'
        assert _extract_error_tip(html) == "账号被锁定"

    def test_找不到时返回_None(self):
        assert _extract_error_tip("<html></html>") is None


class TestSession:
    def test_往返序列化(self, tmp_path):
        session = Session(
            token="tok",
            cookies={"JSESSIONID": "abc"},
            student_name="张三",
            student_code="1120200001",
            origin="sso",
        )
        path = tmp_path / "s.json"
        session.save(path)
        loaded = Session.load(path)
        assert loaded is not None
        assert loaded.token == "tok"
        assert loaded.cookies == {"JSESSIONID": "abc"}
        assert loaded.student_name == "张三"

    def test_会话文件权限收紧(self, tmp_path):
        path = tmp_path / "s.json"
        Session(token="t").save(path)
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_文件不存在返回_None(self, tmp_path):
        assert Session.load(tmp_path / "nope.json") is None

    def test_损坏文件返回_None_而不抛异常(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{不是合法 json")
        assert Session.load(path) is None

    def test_新鲜度判断(self):
        assert Session(token="t").is_probably_fresh()
        old = Session(token="t", created_at=0)
        assert not old.is_probably_fresh()


class TestManualImport:
    def test_从完整回跳链接提取_token(self):
        url = "https://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/bitXsxkLogin/casLogin.do?bitXsxkLogin=SECRET123"
        session = BitAuth.from_manual(token=url)
        assert session.token == "SECRET123"

    def test_解析_cookie_字符串(self):
        cookie = "JSESSIONID=A1B2; route=node1; SERVERID=x"
        session = BitAuth.from_manual(token="tok", cookie=cookie)
        assert session.cookies == {"JSESSIONID": "A1B2", "route": "node1", "SERVERID": "x"}

    def test_容忍_token_前缀与空格(self):
        assert BitAuth.from_manual(token="  Token: abc  ").token == "abc"

    def test_忽略不合法_cookie_片段(self):
        session = BitAuth.from_manual(token="t", cookie="A=1; garbage; B=2")
        assert session.cookies == {"A": "1", "B": "2"}

    def test_空_cookie_得到空字典(self):
        assert BitAuth.from_manual(token="t").cookies == {}


class TestLoginPageParsing:
    """用一份仿真的登录页 HTML 验证解析与异常分支。"""

    PAGE = """
    <html><body>
      <p id="current-login-type">UsernamePassword</p>
      <p id="login-croypto">MDEyMzQ1Njc4OWFiY2RlZg==</p>
      <p id="login-page-flowkey">uuid-1234_ZXlKaGJHY2lPaUpJVXpVeE1pSXNJblI1Y0NJNklrcFhWQ0o5</p>
      <p id="frontend-addr">https://sso.bit.edu.cn/gate</p>
    </body></html>
    """

    def test_解析出密钥与执行串(self):
        auth = BitAuth(HttpClient(min_interval=0))
        html = self.PAGE
        croypto = _parse_tag_value(html, "login-croypto")
        flowkey = _parse_tag_value(html, "login-page-flowkey")
        assert croypto == "MDEyMzQ1Njc4OWFiY2RlZg=="
        assert flowkey.startswith("uuid-1234_")

    def test_页面结构变更时给出可操作报错(self):
        auth = BitAuth(HttpClient(min_interval=0))

        class FakeResp:
            status_code = 200
            text = "<html>完全不一样的页面</html>"

        auth.http.get = lambda *a, **k: FakeResp()  # type: ignore[assignment]
        with pytest.raises(LoginError, match="登录页结构已变更"):
            auth.login("u", "p")

    def test_空账号密码直接报错(self):
        auth = BitAuth(HttpClient(min_interval=0))
        with pytest.raises(LoginError, match="学号或密码为空"):
            auth.login("", "")
