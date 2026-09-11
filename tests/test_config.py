"""配置模块测试：TOML 解析、校验、环境变量覆盖。"""

from __future__ import annotations

import pytest

from bitxk.client import CourseType
from bitxk.config import Config, PollConfig, WatchTarget, load_config
from bitxk.exceptions import ConfigError


def write_config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """
[account]
username = "1120200001"
password = "secret"

[[courses]]
name = "科幻文学"
type = "XGXK"
"""


class TestLoadConfig:
    def test_加载最小配置(self, tmp_path):
        cfg = load_config(write_config(tmp_path, MINIMAL))
        assert cfg.username == "1120200001"
        assert cfg.password == "secret"
        assert len(cfg.courses) == 1
        assert cfg.courses[0].name == "科幻文学"
        assert cfg.courses[0].type == CourseType.PUBLIC

    def test_默认值合理(self, tmp_path):
        cfg = load_config(write_config(tmp_path, MINIMAL))
        assert cfg.poll.interval == 2.0
        assert cfg.poll.min_request_interval == 0.8
        assert cfg.http.timeout == 10.0

    def test_文件不存在时报错(self, tmp_path):
        with pytest.raises(ConfigError, match="不存在"):
            load_config(tmp_path / "nope.toml")

    def test_toml_语法错误时报错(self, tmp_path):
        path = write_config(tmp_path, "[[[ broken")
        with pytest.raises(ConfigError, match="语法错误"):
            load_config(path)

    def test_未知配置项报错(self, tmp_path):
        body = MINIMAL + "\n[poll]\nintervall = 3.0\n"
        with pytest.raises(ConfigError, match="未知配置项"):
            load_config(write_config(tmp_path, body))

    def test_课程段不是数组时精确报错(self, tmp_path):
        path = write_config(tmp_path, MINIMAL.replace("[[courses]]", "[courses]"))
        with pytest.raises(ConfigError, match="必须是数组表"):
            load_config(path)

    def test_base_dir指向配置文件所在目录(self, tmp_path):
        cfg = load_config(write_config(tmp_path, MINIMAL))
        assert cfg.base_dir == tmp_path.resolve()


class TestWatchTarget:
    def test_非法类型报错并提示可选值(self):
        with pytest.raises(ConfigError, match="非法"):
            WatchTarget(name="某课", type="NOPE")

    def test_空名称报错(self):
        with pytest.raises(ConfigError, match="不能为空"):
            WatchTarget(name="   ")

    def test_类型自动大写(self):
        assert WatchTarget(name="x", type="xgxk").type == "XGXK"

    def test_老师支持逗号分隔字符串(self):
        target = WatchTarget(name="x", teachers="张三, 李四")
        assert target.teachers == ["张三", "李四"]

    def test_老师筛选是子串匹配(self):
        target = WatchTarget(name="x", teachers=["张三"])

        class FakeTc:
            teacher = "张三丰"
            teaching_class_id = "1"

        assert target.matches(FakeTc())

    def test_指定教学班时忽略老师条件(self):
        target = WatchTarget(name="x", teachers=["张三"], classes=["999"])

        class FakeTc:
            teacher = "别人"
            teaching_class_id = "999"

        assert target.matches(FakeTc())

    def test_无筛选条件时全部匹配(self):
        target = WatchTarget(name="x")

        class FakeTc:
            teacher = ""
            teaching_class_id = "1"

        assert target.matches(FakeTc())


class TestPollConfig:
    def test_间隔过小被拒绝(self):
        with pytest.raises(ConfigError, match="太小"):
            PollConfig(interval=0.1)

    def test_最小请求间隔过小被拒绝(self):
        with pytest.raises(ConfigError, match="不得低于"):
            PollConfig(min_request_interval=0.05)

    def test_错误阈值必须为正(self):
        with pytest.raises(ConfigError, match="必须大于"):
            PollConfig(max_consecutive_errors=0)


class TestConfigValidate:
    def test_缺学号报错并提示环境变量方案(self, tmp_path):
        body = MINIMAL.replace('username = "1120200001"', 'username = ""')
        cfg = load_config(write_config(tmp_path, body))
        with pytest.raises(ConfigError, match="BITXK_USERNAME"):
            cfg.validate()

    def test_无课程报错(self, tmp_path):
        body = '[account]\nusername = "x"\npassword = "y"\n'
        cfg = load_config(write_config(tmp_path, body))
        with pytest.raises(ConfigError, match="未配置任何课程"):
            cfg.validate()

    def test_全部禁用报错(self, tmp_path):
        body = MINIMAL + "enabled = false\n"
        cfg = load_config(write_config(tmp_path, body))
        with pytest.raises(ConfigError, match="都被禁用"):
            cfg.validate()

    def test_优先级排序(self):
        cfg = Config(
            username="x",
            courses=[
                WatchTarget(name="低", priority=200),
                WatchTarget(name="高", priority=10),
                WatchTarget(name="中", priority=100),
            ],
        )
        assert [c.name for c in cfg.enabled_courses] == ["高", "中", "低"]

    def test_禁用的课不参与排序(self):
        cfg = Config(username="x", courses=[
            WatchTarget(name="A", enabled=False),
            WatchTarget(name="B"),
        ])
        assert [c.name for c in cfg.enabled_courses] == ["B"]


class TestEnvOverride:
    def test_环境变量覆盖账号密码(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BITXK_USERNAME", "envuser")
        monkeypatch.setenv("BITXK_PASSWORD", "envpass")
        cfg = load_config(write_config(tmp_path, MINIMAL))
        assert cfg.username == "envuser"
        assert cfg.password == "envpass"

    def test_insecure_开关关闭证书校验(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BITXK_INSECURE", "1")
        cfg = load_config(write_config(tmp_path, MINIMAL))
        assert cfg.http.verify_ssl is False

    def test_未设置环境变量时保留文件值(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BITXK_USERNAME", raising=False)
        monkeypatch.delenv("BITXK_PASSWORD", raising=False)
        cfg = load_config(write_config(tmp_path, MINIMAL))
        assert cfg.username == "1120200001"
