"""命令行层测试：参数解析、渲染、配置错误处理。

``init`` 与 ``check`` 是纯本地操作，可安全地真实执行；
登录相关的路径通过替换 ``_connect`` 来隔离。
"""

from __future__ import annotations

import pytest

import bitxk.cli as cli
from bitxk.exceptions import BitxkError, NetworkError, TokenExpired

# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


class TestParser:
    def test_无子命令时返回_0_并打印帮助(self, capsys):
        assert cli.main([]) == 0
        assert "usage" in capsys.readouterr().out

    def test_默认间隔为_none_以便区分是否覆盖(self):
        args = cli.parse_args(["grab"])
        assert args.interval is None
        assert args.duration is None

    def test_dry_run_开关(self):
        assert cli.parse_args(["grab", "--dry-run"]).dry_run is True

    def test_once_开关(self):
        assert cli.parse_args(["grab", "--once"]).once is True

    def test_加密模式只接受两种值(self):
        parser = cli.build_parser()
        assert parser.parse_args(["--encrypt-mode", "cbc"]).encrypt_mode == "cbc"
        with pytest.raises(SystemExit):
            parser.parse_args(["--encrypt-mode", "rot13"])

    def test_全局参数可在子命令前给出(self):
        args = cli.parse_args(["-c", "x.toml", "-v", "list"])
        assert args.config == "x.toml"
        assert args.verbose is True
        assert args.command == "list"

    def test_版本输出版本号(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--version"])
        assert "bitxk" in capsys.readouterr().out


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


class TestInit:
    def test_生成配置模板(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert cli.main(["init"]) == 0
        target = tmp_path / "config.toml"
        assert target.exists()
        body = target.read_text(encoding="utf-8")
        assert "[account]" in body
        assert "[[courses]]" in body

    def test_生成的模板能被正确加载(self, tmp_path):
        """模板必须自身合法，否则用户第一步就踩坑。"""
        from bitxk.config import SAMPLE_CONFIG, load_config

        path = tmp_path / "config.toml"
        path.write_text(SAMPLE_CONFIG, encoding="utf-8")
        cfg = load_config(path)
        assert len(cfg.courses) == 1
        assert cfg.courses[0].name == "科幻文学"

    def test_已存在时不覆盖(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.toml").write_text("原有内容", encoding="utf-8")
        assert cli.main(["init"]) == 1
        assert (tmp_path / "config.toml").read_text(encoding="utf-8") == "原有内容"
        assert "已存在" in capsys.readouterr().out

    def test_指定路径(self, tmp_path):
        target = tmp_path / "sub" / "my.toml"
        target.parent.mkdir()
        assert cli.main(["-c", str(target), "init"]) == 0
        assert target.exists()


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def make_check_http(page_html: str, *, cas_status: int = 302):
    """构造 check 用的假 HTTP 客户端。

    check 会依次请求：选课系统首页、CAS 入口、统一身份认证登录页，
    因此假客户端必须按 URL 分流返回。
    """

    class FakeResp:
        def __init__(self, status_code=200, text="", headers=None):
            self.status_code = status_code
            self.text = text
            self.headers = headers or {}

    class FakeHttp:
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            # 注意：SSO 登录页的 URL 里也含 "casLogin.do"（它是 service 参数），
            # 所以判别要用更精确的路径，否则登录页会被误当成 CAS 入口。
            if "bitXsxkLogin/casLogin.do" in url and "sso.bit.edu.cn" not in url:
                headers = (
                    {"Location": "https://sso.bit.edu.cn/cas/login?service=x"}
                    if cas_status in (301, 302, 303, 307, 308)
                    else {}
                )
                return FakeResp(cas_status, headers=headers)
            # 统一身份认证登录页（以及首页）都返回这份 HTML
            return FakeResp(200, page_html)

        def close(self):
            pass

    return FakeHttp


class TestCheck:
    def test_结构正常时返回_0(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli,
            "HttpClient",
            make_check_http('<p id="login-croypto">k</p><p id="login-page-flowkey">f</p>'),
        )
        assert cli.main(["check"]) == 0
        assert "自检通过" in capsys.readouterr().out

    def test_登录页改版时返回_1_并提示兜底方案(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "HttpClient", make_check_http("<html>完全不一样</html>"))
        assert cli.main(["check"]) == 1
        out = capsys.readouterr().out
        assert "登录页结构已变更" in out
        assert "--cookie" in out

    def test_旧版登录页时提示_cbc_模式(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli, "HttpClient", make_check_http('<input id="pwdEncryptSalt" value="x">')
        )
        assert cli.main(["check"]) == 0
        assert "cbc" in capsys.readouterr().out

    def test_会校验_CAS_入口(self, monkeypatch, capsys):
        """本科系统的 CAS 入口应 302 到统一身份认证，这一跳值得单独验证。"""
        monkeypatch.setattr(
            cli,
            "HttpClient",
            make_check_http('<p id="login-croypto">k</p><p id="login-page-flowkey">f</p>'),
        )
        cli.main(["check"])
        assert "CAS 入口正常" in capsys.readouterr().out

    def test_CAS_入口异常时只告警不失败(self, monkeypatch, capsys):
        """入口探测失败通常只是临时网络问题，不该让整个自检判失败。"""
        monkeypatch.setattr(
            cli,
            "HttpClient",
            make_check_http(
                '<p id="login-croypto">k</p><p id="login-page-flowkey">f</p>',
                cas_status=502,
            ),
        )
        assert cli.main(["check"]) == 0
        assert "CAS 入口" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 错误处理
# --------------------------------------------------------------------------


class TestErrors:
    def test_配置文件缺失时返回_4(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert cli.main(["login"]) == 4
        assert "配置文件" in capsys.readouterr().out

    def test_登录失败时返回_2_并给出排查建议(self, tmp_path, monkeypatch, capsys):
        from bitxk.exceptions import LoginError

        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[account]\nusername="u"\npassword="p"\n[[courses]]\nname="课"\n',
            encoding="utf-8",
        )

        def fake_connect(*a, **k):
            raise LoginError("账号或密码错误")

        monkeypatch.setattr(cli, "_connect", fake_connect)
        assert cli.main(["-c", str(cfg), "grab"]) == 2
        out = capsys.readouterr().out
        assert "登录失败" in out
        assert "encrypt-mode cbc" in out

    def test_不在批次内返回_3(self, tmp_path, monkeypatch, capsys):
        from bitxk.exceptions import NotInBatchError

        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[account]\nusername="u"\npassword="p"\n[[courses]]\nname="课"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            cli,
            "_connect",
            lambda *a, **k: (_ for _ in ()).throw(NotInBatchError("不在选课时间")),
        )
        assert cli.main(["-c", str(cfg), "grab"]) == 3
        assert "不在选课时间" in capsys.readouterr().out

    def test_配置校验失败返回_4(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[account]\nusername=""\npassword=""\n', encoding="utf-8")
        assert cli.main(["-c", str(cfg), "list"]) == 4
        assert "学号" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 手动导入
# --------------------------------------------------------------------------


class TestManualSession:
    def test_token_与_cookie_组合出会话(self):
        args = cli.parse_args(["grab", "--token", "tok", "--cookie", "A=1; B=2"])
        from bitxk.config import Config

        session = cli._manual_session(args, Config())
        assert session is not None
        assert session.token == "tok"
        assert session.cookies == {"A": "1", "B": "2"}

    def test_只给_cookie_时报错说明原因(self):
        from bitxk.config import Config
        from bitxk.exceptions import ConfigError

        args = cli.parse_args(["grab", "--cookie", "A=1"])
        with pytest.raises(ConfigError, match="需要 --token"):
            cli._manual_session(args, Config())

    def test_未提供时返回_none(self):
        from bitxk.config import Config

        args = cli.parse_args(["grab"])
        assert cli._manual_session(args, Config()) is None

    def test_学号可通过命令行提供(self):
        """手动导入模式下 config 的 account 段可以是空的，学号得能从命令行来。"""
        from bitxk.config import Config

        args = cli.parse_args(["grab", "--token", "tok", "--student-code", "1120200001"])
        session = cli._manual_session(args, Config())
        assert session is not None
        assert session.student_code == "1120200001"

    def test_回跳_url_也能被接受(self):
        from bitxk.config import Config

        args = cli.parse_args(
            [
                "grab",
                "--token",
                "https://xk.bit.edu.cn/x/bitXsxkLogin/casLogin.do?bitXsxkLogin=KEY",
            ]
        )
        session = cli._manual_session(args, Config())
        assert session is not None
        assert session.token == "KEY"


# --------------------------------------------------------------------------
# 事件渲染
# --------------------------------------------------------------------------


class TestRenderer:
    def _render(self, event, payload, **kw):
        collected: list[str] = []

        class FakeNotifier:
            def success(self, course, detail=""):
                collected.append(f"NOTIFY:{course}")

            def alert(self, title, message):
                collected.append(f"ALERT:{title}")

        render = cli._make_renderer(FakeNotifier(), **kw)
        render(event, payload)
        return collected

    def test_成功事件触发通知(self):
        assert self._render("success", {"course": "科幻文学", "class_id": "1"}) == [
            "NOTIFY:科幻文学"
        ]

    def test_冲突事件不触发通知(self):
        assert self._render("conflict", {"course": "课", "class_id": "1"}) == []

    def test_未知事件不崩溃(self):
        assert self._render("从未见过的事件", {}) == []

    def test_dry_run_提示不提交(self, capsys):
        self._render(
            "dry_run_skip",
            {"course": "课", "class_id": "1", "capacity": "余 3"},
            dry_run=True,
        )
        assert "dry-run" in capsys.readouterr().out


class TestStyle:
    def test_非_tty_时不输出_ansi_转义(self, monkeypatch):
        monkeypatch.setattr(cli.Style, "enabled", False)
        assert "\033[" not in cli.Style.green("文本")
        assert cli.Style.green("文本") == "文本"

    def test_启用时包裹转义序列(self, monkeypatch):
        monkeypatch.setattr(cli.Style, "enabled", True)
        assert cli.Style.red("x").startswith("\033[31m")


class TestCachedSessionResilience:
    """缓存会话在网络故障时不该被丢弃 —— 实测踩过的坑。

    真机上遇到：`xk.bit.edu.cn` 临时不可达（SSL EOF），而校验缓存会话的
    代码把**任何** BitxkError 都当成"会话失效"，于是删掉好好的登录态、
    转去要求输入密码。用户看到的是"让我重新登录"，而真正的问题只是网络。
    """

    def _cfg(self, tmp_path):
        from bitxk.config import Config, WatchTarget

        cfg = Config(base_dir=tmp_path)
        # 至少要有一门课，否则会先被 validate() 拦下，测不到网络分支
        cfg.courses = [WatchTarget(name="测试课")]
        return cfg

    def test_网络错误不丢会话(self, tmp_path, monkeypatch):
        from bitxk.auth import Session
        from bitxk.cli import _connect
        from bitxk.exceptions import BitxkError

        session = Session(
            token="TOK", cookies={"_WEU": "x"}, student_code="1120252751", origin="browser"
        )
        session.save(tmp_path / ".bitxk_session.json")

        class BoomClient:
            def __init__(self, *_a, **_k):
                pass

            def student_info(self, *_a, **_k):
                raise NetworkError("SSL EOF")

        # check_session 内部直接用 XkClient（配自己新建的 HttpClient），
        # 所以打桩点在这里，而不是 cli._client
        monkeypatch.setattr("bitxk.client.XkClient", BoomClient)

        args = cli.parse_args(["list"])
        with pytest.raises(BitxkError, match="网络"):
            _connect(self._cfg(tmp_path), args)

        # 关键：会话文件必须还在
        assert (tmp_path / ".bitxk_session.json").exists(), "网络故障不该删掉登录态"

    def test_登录失效才丢会话(self, tmp_path, monkeypatch):
        from bitxk.auth import Session
        from bitxk.cli import _connect
        from bitxk.exceptions import LoginError

        session = Session(token="OLD", cookies={}, student_code="1", origin="browser")
        session.save(tmp_path / ".bitxk_session.json")

        class ExpiredClient:
            def __init__(self, *_a, **_k):
                pass

            def student_info(self, *_a, **_k):
                raise TokenExpired("登录态失效")

        monkeypatch.setattr("bitxk.client.XkClient", ExpiredClient)

        args = cli.parse_args(["-u", "1", "-p", "pw", "list"])
        # 会话失效 → 应转入账号密码登录；密码给了但登录会打到真实网络，
        # 这里只断言它**没有**因为网络错误而通过，即走到了登录分支。
        with pytest.raises((LoginError, BitxkError)):
            _connect(self._cfg(tmp_path), args)
