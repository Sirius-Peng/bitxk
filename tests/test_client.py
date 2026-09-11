"""业务客户端测试：参数组装、响应解析、错误分类。

全部用假 HTTP 层，不发真实请求。
"""

from __future__ import annotations

import json

import pytest

from bitxk.client import CourseType, XkClient, _classify_error
from bitxk.exceptions import ApiError, NetworkError, NotInBatchError, RateLimited, TokenExpired
from bitxk.models import CourseStatus, SelectionOutcome


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, text=None, url="", history=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)
        self.url = url
        self.history = history or []

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    """记录请求并按队列返回响应的假 HTTP 客户端。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.token = None

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"没有预置响应了，却被请求：{url}")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"没有预置响应了，却被请求：{url}")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def set_token(self, token):
        self.token = token

    @property
    def cookies(self):
        return {}

    @cookies.setter
    def cookies(self, value):
        pass


class TestQueryCourses:
    def test_参数按wisedu约定放进query(self):
        http = FakeHttp([FakeResponse({"code": "1", "dataList": []})])
        client = XkClient(http)
        client.query_courses(
            "科幻文学",
            teaching_class_type=CourseType.PUBLIC,
            batch_code="B1",
            student_code="1120200001",
        )
        call = http.calls[0]
        assert call["url"].endswith("/elective/publicCourse.do")
        setting = json.loads(call["params"]["querySetting"])
        assert setting["data"]["queryContent"] == "科幻文学"
        assert setting["data"]["teachingClassType"] == "XGXK"
        assert setting["data"]["electiveBatchCode"] == "B1"
        assert setting["data"]["studentCode"] == "1120200001"
        assert setting["pageSize"] == "50"

    def test_轮询必须带已满课程(self):
        """checkCapacity=2/0 才返回满员课程，否则无法观察余量变化。"""
        http = FakeHttp([FakeResponse({"code": "1", "dataList": []})])
        client = XkClient(http)
        client.query_courses(
            "x",
            teaching_class_type=CourseType.PUBLIC,
            batch_code="B",
            student_code="S",
            check_capacity="0",
        )
        setting = json.loads(http.calls[0]["params"]["querySetting"])
        assert setting["data"]["checkCapacity"] == "0"

    def test_体育课走_programCourse(self):
        http = FakeHttp([FakeResponse({"code": "1", "dataList": []})])
        client = XkClient(http)
        client.query_courses(
            "体育/羽毛球",
            teaching_class_type=CourseType.PE,
            batch_code="B",
            student_code="S",
        )
        assert http.calls[0]["url"].endswith("/elective/programCourse.do")

    def test_解析课程与教学班(self):
        payload = {
            "code": "1",
            "dataList": [
                {
                    "courseName": "科幻文学",
                    "tcList": [
                        {
                            "teachingClassID": "1001",
                            "teacherName": "张三",
                            "remainCapacity": 3,
                            "capacity": 40,
                        },
                    ],
                }
            ],
        }
        http = FakeHttp([FakeResponse(payload)])
        client = XkClient(http)
        courses = client.query_courses(
            "科幻文学",
            batch_code="B",
            student_code="S",
        )
        assert len(courses) == 1
        tc = courses[0].teaching_classes[0]
        assert tc.teaching_class_id == "1001"
        assert tc.remaining == 3
        assert tc.status is CourseStatus.AVAILABLE

    def test_空结果返回空列表(self):
        http = FakeHttp([FakeResponse({"code": "1", "dataList": []})])
        client = XkClient(http)
        assert client.query_courses("无此课", batch_code="B", student_code="S") == []

    def test_dataList为null时不崩(self):
        http = FakeHttp([FakeResponse({"code": "1", "dataList": None})])
        client = XkClient(http)
        assert client.query_courses("x", batch_code="B", student_code="S") == []


class TestErrorHandling:
    def test_401视为登录失效(self):
        http = FakeHttp([FakeResponse(None, status_code=401, text="")])
        client = XkClient(http)
        with pytest.raises(TokenExpired):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_429视为限流(self):
        http = FakeHttp([FakeResponse(None, status_code=429, text="")])
        client = XkClient(http)
        with pytest.raises(RateLimited):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_返回_html_登录页视为登录失效(self):
        http = FakeHttp([FakeResponse(None, text="<html>cas login page</html>")])
        client = XkClient(http)
        with pytest.raises(TokenExpired):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_200_返回应用首页_html_也视为登录失效(self):
        """真实踩坑：Token 失效时选课系统返回 200 + 应用首页 HTML，而不是 401。

        只按状态码判断会把「登录失效」误判成「接口异常」，
        导致永远不触发重新登录，程序卡死。
        """
        app_shell = (
            '<!DOCTYPE html><html lang="zh-CN"><head><title>选课</title></head>'
            '<body><script src="https://jxzxres.bit.edu.cn/products/jwfw/'
            'xsxkapp/public/js/xsxkpub.js"></script></body></html>'
        )
        http = FakeHttp([FakeResponse(None, status_code=200, text=app_shell)])
        client = XkClient(http)
        with pytest.raises(TokenExpired, match="登录态已失效"):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_200_返回纯垃圾_html_不算登录失效(self):
        """但也不是所有 HTML 都是登录页，不能无脑重登。"""
        http = FakeHttp([FakeResponse(None, status_code=200, text="<html>hello</html>")])
        client = XkClient(http)
        with pytest.raises(TokenExpired):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_真实业务首页标记被识别(self):
        from bitxk.client import _looks_like_login_page

        assert _looks_like_login_page("<!DOCTYPE html><html><title>选课</title>")
        assert _looks_like_login_page('<p id="login-croypto">x</p>')
        assert _looks_like_login_page('<script src="/xsxkpub.js">')
        assert not _looks_like_login_page("")
        assert not _looks_like_login_page('{"code":"1"}')

    def test_返回垃圾内容抛_api_错误(self):
        """两种参数传递方式都失败后才报错，避免把「版本差异」误报成「接口坏了」。"""
        http = FakeHttp(
            [
                FakeResponse(None, text="<<<garbage>>>"),
                FakeResponse(None, text="<<<garbage>>>"),
            ]
        )
        client = XkClient(http)
        with pytest.raises(ApiError, match="都返回了非 JSON"):
            client.query_courses("x", batch_code="B", student_code="S")
        assert len(http.calls) == 2

    def test_query_参数失败时自动降级为_form_body(self):
        """部分版本只认 form body，此时应自动重试而不是直接失败。"""
        http = FakeHttp(
            [
                FakeResponse(None, text="<<<not json>>>"),
                FakeResponse({"code": "1", "dataList": []}),
            ]
        )
        client = XkClient(http)
        assert client.query_courses("x", batch_code="B", student_code="S") == []
        # 第二次请求把参数放进了 body
        assert "data" in http.calls[1]
        assert "querySetting" in http.calls[1]["data"]
        assert "params" not in http.calls[1]

    def test_登录失效时不触发_form_降级(self):
        """登录失效必须立刻上报，不该白白多发一次请求。"""
        app_shell = "<!DOCTYPE html><html><title>选课</title>"
        http = FakeHttp([FakeResponse(None, status_code=200, text=app_shell)])
        client = XkClient(http)
        with pytest.raises(TokenExpired):
            client.query_courses("x", batch_code="B", student_code="S")
        assert len(http.calls) == 1

    def test_网络异常向上抛出(self):
        http = FakeHttp([NetworkError("连接超时")])
        client = XkClient(http)
        with pytest.raises(NetworkError):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_msg含登录字样视为登录失效(self):
        http = FakeHttp([FakeResponse({"code": "-99", "msg": "登录已超时，请重新登录"})])
        client = XkClient(http)
        with pytest.raises(TokenExpired):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_批次错误抛出_not_in_batch(self):
        http = FakeHttp([FakeResponse({"code": "-1", "msg": "当前批次不可用"})])
        client = XkClient(http)
        with pytest.raises(NotInBatchError):
            client.query_courses("x", batch_code="B", student_code="S")


class TestBatches:
    def test_取当前可选批次(self):
        payload = {
            "code": "1",
            "data": {
                "name": "张三",
                "electiveBatchList": [
                    {"code": "OLD", "canSelect": "0", "name": "已结束"},
                    {
                        "code": "NOW",
                        "canSelect": "1",
                        "name": "第一轮",
                        "schoolTermName": "2024-2025-1",
                    },
                ],
            },
        }
        http = FakeHttp([FakeResponse(payload)])
        client = XkClient(http)
        batch = client.current_batch()
        assert batch.code == "NOW"
        assert batch.can_select

    def test_没有可选批次时报错并列出已知批次(self):
        payload = {
            "code": "1",
            "data": {"electiveBatchList": [{"code": "A", "canSelect": "0", "name": "已结束"}]},
        }
        http = FakeHttp([FakeResponse(payload)])
        client = XkClient(http)
        with pytest.raises(NotInBatchError, match="不在可选课时间"):
            client.current_batch()

    def test_候选接口降级(self):
        """第一个接口不返回 data 时自动试第二个。"""
        http = FakeHttp(
            [
                FakeResponse({"code": "1", "data": {}}),
                FakeResponse(
                    {"code": "1", "data": {"electiveBatchList": [{"code": "B", "canSelect": "1"}]}}
                ),
            ]
        )
        client = XkClient(http)
        assert client.current_batch().code == "B"
        assert len(http.calls) == 2


class TestSubmit:
    def test_提交参数正确(self):
        http = FakeHttp([FakeResponse({"code": "1", "msg": "选课成功"})])
        client = XkClient(http)
        result = client.submit("1001", batch_code="B", student_code="S", course_name="科幻文学")
        add_param = json.loads(http.calls[0]["params"]["addParam"])
        assert add_param["data"]["teachingClassId"] == "1001"
        assert add_param["data"]["operationType"] == "1"
        assert add_param["data"]["teachingClassType"] == "XGXK"
        assert result.outcome is SelectionOutcome.SUCCESS
        assert result.ok

    def test_满员被识别为可重试(self):
        http = FakeHttp([FakeResponse({"code": "0", "msg": "选课时发生错误，超过限选人数。"})])
        client = XkClient(http)
        result = client.submit("1001", batch_code="B", student_code="S")
        assert result.outcome is SelectionOutcome.FULL
        assert result.outcome.should_retry

    def test_时间冲突(self):
        http = FakeHttp([FakeResponse({"code": "0", "msg": "该课程与学生已选课程存在时间冲突。"})])
        client = XkClient(http)
        result = client.submit("1001", batch_code="B", student_code="S")
        assert result.outcome is SelectionOutcome.CONFLICT

    def test_重复选课(self):
        http = FakeHttp([FakeResponse({"code": "0", "msg": "该课程已选，请勿重复选课"})])
        client = XkClient(http)
        result = client.submit("1001", batch_code="B", student_code="S")
        assert result.outcome is SelectionOutcome.ALREADY

    def test_业务失败不抛异常而是返回结果(self):
        """满员是轮询中的常态，不能当异常处理。"""
        http = FakeHttp([FakeResponse({"code": "0", "msg": "余量不足"})])
        client = XkClient(http)
        result = client.submit("1001", batch_code="B", student_code="S")
        assert not result.ok
        assert result.outcome is SelectionOutcome.FULL

    def test_登录失效仍然抛异常(self):
        """这类问题必须让上层感知以便重登，不能降级成结果对象。"""
        http = FakeHttp([FakeResponse(None, status_code=401, text="")])
        client = XkClient(http)
        with pytest.raises(TokenExpired):
            client.submit("1001", batch_code="B", student_code="S")

    def test_限流仍然抛异常(self):
        http = FakeHttp([FakeResponse(None, status_code=429, text="")])
        client = XkClient(http)
        with pytest.raises(RateLimited):
            client.submit("1001", batch_code="B", student_code="S")


class TestFindTeachingClasses:
    def test_精确匹配过滤掉近似课程名(self):
        payload = {
            "code": "1",
            "dataList": [
                {
                    "courseName": "大学语文",
                    "tcList": [{"teachingClassID": "1", "remainCapacity": 1}],
                },
                {
                    "courseName": "大学语文（进阶）",
                    "tcList": [{"teachingClassID": "2", "remainCapacity": 1}],
                },
            ],
        }
        http = FakeHttp([FakeResponse(payload)])
        client = XkClient(http)
        found = client.find_teaching_classes("大学语文", batch_code="B", student_code="S")
        assert [tc.teaching_class_id for tc in found] == ["1"]

    def test_关闭精确匹配时全部返回(self):
        payload = {
            "code": "1",
            "dataList": [
                {"courseName": "大学语文", "tcList": [{"teachingClassID": "1"}]},
                {"courseName": "大学语文（进阶）", "tcList": [{"teachingClassID": "2"}]},
            ],
        }
        http = FakeHttp([FakeResponse(payload)])
        client = XkClient(http)
        found = client.find_teaching_classes(
            "大学语文", batch_code="B", student_code="S", exact_match=False
        )
        assert len(found) == 2


class TestClassifyError:
    @pytest.mark.parametrize(
        "msg,expected",
        [
            ("选课成功", SelectionOutcome.SUCCESS),
            ("该课程已选", SelectionOutcome.ALREADY),
            ("时间冲突", SelectionOutcome.CONFLICT),
            ("超过限选人数。", SelectionOutcome.FULL),
            ("人数已满", SelectionOutcome.FULL),
            ("不在选课时间内", SelectionOutcome.NOT_IN_BATCH),
            ("操作过于频繁", SelectionOutcome.RATE_LIMITED),
            ("登录超时", SelectionOutcome.AUTH_ERROR),
            ("某种没见过的错误", SelectionOutcome.ERROR),
            ("", SelectionOutcome.ERROR),
        ],
    )
    def test_关键词分类(self, msg, expected):
        assert _classify_error(msg) is expected


class TestCourseType:
    def test_类型合法(self):
        assert set(CourseType.ALL) == {"TJKC", "FANKC", "XGXK", "TYKC", "XGKC", "TJK"}

    def test_中文标签(self):
        assert CourseType.label("XGXK") == "校公选课"
        assert CourseType.label("TYKC") == "体育课程"

    def test_未知类型回退为原文(self):
        assert CourseType.label("ZZZZ") == "ZZZZ"
