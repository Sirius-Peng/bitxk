"""业务客户端测试。

覆盖的生产契约（均来自对前端源码的逆向，不是推测）：

* 参数以 **POST 表单体**发送，键名 ``querySetting`` / ``addParam`` / ``deleteParam``；
* 鉴权头是全小写 ``token``；
* 批次来自 ``student/<学号>.do``；
* 余量没有现成字段，需 ``classCapacity - 已选人数`` 推算；
* **选课是异步的**：``volunteer.do`` 受理后必须轮询 ``studentstatus.do``。

全部使用假 HTTP 层，不发真实请求。
"""

from __future__ import annotations

import json

import pytest

from bitxk.client import CourseType, XkClient, _classify_error
from bitxk.exceptions import ApiError, NetworkError, NotInBatchError, RateLimited, TokenExpired
from bitxk.models import CourseStatus, SelectionOutcome


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, text=None, url="", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)
        self.url = url
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeHttp:
    """记录请求并按队列返回响应的假 HTTP 客户端。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.token = None
        self.student_code = "1120200001"

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


def form_body(call: dict) -> dict:
    """取请求的表单体参数（新契约用 POST 表单，不再是 URL query）。"""
    return call.get("data") or {}


def make_client(*responses, **kwargs):
    http = FakeHttp(list(responses))
    return XkClient(http, **kwargs), http


def envelope(**fields):
    """构造一个标准响应信封（已包成 HTTP 响应对象）：``code`` 是**字符串**。"""
    base = {"data": None, "msg": "", "code": "1", "map": None, "timestamp": "0"}
    base.update(fields)
    return FakeResponse(base)


def token_expired_response():
    """token 失效的真实形态：HTTP 401 + text/html "Not login!"。"""
    return FakeResponse(
        None,
        status_code=401,
        text="<!DOCTYPE html><html><head><title>401</title></head><body>Not login!</body></html>",
    )


# --------------------------------------------------------------------------
# 课程查询
# --------------------------------------------------------------------------


class TestQueryCourses:
    def test_参数放进_POST_表单体(self):
        client, http = make_client(envelope(dataList=[]))
        client.query_courses(
            "科幻文学",
            teaching_class_type=CourseType.XGXK,
            batch_code="B1",
            student_code="1120200001",
        )
        call = http.calls[0]
        assert call["url"].endswith("/elective/publicCourse.do")
        setting = json.loads(form_body(call)["querySetting"])
        assert setting["data"]["queryContent"] == "科幻文学"
        assert setting["data"]["teachingClassType"] == "XGXK"
        assert setting["data"]["electiveBatchCode"] == "B1"
        assert setting["data"]["studentCode"] == "1120200001"

    def test_分页字段是字符串(self):
        """前端把 pageSize/pageNumber/order 都作为字符串发送。"""
        client, http = make_client(envelope(dataList=[]))
        client.query_courses("x", batch_code="B", student_code="S")
        setting = json.loads(form_body(http.calls[0])["querySetting"])
        assert isinstance(setting["pageSize"], str)
        assert isinstance(setting["pageNumber"], str)
        assert isinstance(setting["order"], str)

    def test_鉴权头是全小写token(self):
        client, http = make_client(envelope(dataList=[]))
        client.http.token = "TOK"
        client.query_courses("x", batch_code="B", student_code="S")
        headers = http.calls[0]["headers"]
        assert headers["token"] == "TOK"
        assert "Token" not in headers
        assert "language" in headers

    def test_轮询必须用checkCapacity等于2(self):
        """'2' = 校验但不过滤，满员课仍留在列表里，才能观察余量翻转。

        用 '1' 时满员课会从列表消失，就永远等不到放课。
        """
        client, http = make_client(envelope(dataList=[]))
        client.query_courses("x", batch_code="B", student_code="S")
        setting = json.loads(form_body(http.calls[0])["querySetting"])
        assert setting["data"]["checkCapacity"] == "2"
        assert setting["data"]["checkConflict"] == "0"

    @pytest.mark.parametrize(
        ("course_type", "expected_endpoint"),
        [
            (CourseType.XGXK, "elective/publicCourse.do"),
            (CourseType.TJKC, "elective/recommendedCourse.do"),
            (CourseType.FANKC, "elective/programCourse.do"),
            (CourseType.FAWKC, "elective/programCourse.do"),
            (CourseType.CXKC, "elective/programCourse.do"),
            (CourseType.TYKC, "elective/programCourse.do"),
            (CourseType.FXKC, "elective/programCourse.do"),
            (CourseType.QXKC, "elective/course.do"),
        ],
    )
    def test_各类型走正确端点(self, course_type, expected_endpoint):
        """体育课没有独立端点，与方案内/外、重修、辅修共用 programCourse。"""
        client, http = make_client(envelope(dataList=[]))
        client.query_courses("x", teaching_class_type=course_type, batch_code="B", student_code="S")
        assert http.calls[0]["url"].endswith(expected_endpoint)

    def test_辅修课isMajor为0(self):
        client, http = make_client(envelope(dataList=[]))
        client.query_courses(
            "x", teaching_class_type=CourseType.FXKC, batch_code="B", student_code="S"
        )
        setting = json.loads(form_body(http.calls[0])["querySetting"])
        assert setting["data"]["isMajor"] == "0"

    def test_其它类型isMajor为1(self):
        client, http = make_client(envelope(dataList=[]))
        client.query_courses(
            "x", teaching_class_type=CourseType.TYKC, batch_code="B", student_code="S"
        )
        setting = json.loads(form_body(http.calls[0])["querySetting"])
        assert setting["data"]["isMajor"] == "1"

    def test_dataList与data平级(self):
        """列表在信封顶层，不在 data 里 —— 这是容易搞错的地方。"""
        payload = envelope(
            data={"someOther": "thing"},
            dataList=[
                {
                    "courseName": "科幻文学",
                    "classCapacity": "40",
                    "numberOfFirstVolunteer": "10",
                    "teachingClassID": "1001",
                }
            ],
        )
        client, _ = make_client(payload)
        courses = client.query_courses("科幻文学", batch_code="B", student_code="S")
        assert len(courses) == 1

    def test_解析课程与推算余量(self):
        payload = envelope(
            dataList=[
                {
                    "courseName": "科幻文学",
                    "teachingClassID": "1001",
                    "teacherName": "张三",
                    "classCapacity": "40",
                    "numberOfFirstVolunteer": "12",
                }
            ]
        )
        client, _ = make_client(payload)
        courses = client.query_courses("科幻文学", batch_code="B", student_code="S")
        tc = courses[0].teaching_classes[0]
        assert tc.teaching_class_id == "1001"
        assert tc.capacity == 40
        assert tc.selected_count == 12
        assert tc.remaining == 28
        assert tc.capacity_source == "derived"
        assert tc.status is CourseStatus.AVAILABLE

    def test_空结果返回空列表(self):
        client, _ = make_client(envelope(dataList=[]))
        assert client.query_courses("无此课", batch_code="B", student_code="S") == []

    def test_dataList为null时不崩(self):
        client, _ = make_client(envelope(dataList=None))
        assert client.query_courses("x", batch_code="B", student_code="S") == []

    def test_缺学号时报错(self):
        client, http = make_client(envelope(dataList=[]))
        http.student_code = ""
        with pytest.raises(ApiError, match="需要学号"):
            client.query_courses("x", batch_code="B")


# --------------------------------------------------------------------------
# 错误处理
# --------------------------------------------------------------------------


class TestErrorHandling:
    def test_401_html_视为登录失效(self):
        """实测：token 失效返回 401 + text/html，必须先判状态码再解析正文。"""
        client, _ = make_client(token_expired_response())
        with pytest.raises(TokenExpired, match="登录态失效"):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_200_返回应用首页_html_也是登录失效(self):
        app_shell = (
            '<!DOCTYPE html><html lang="zh-CN"><head><title>选课</title></head>'
            '<body><script src="/products/jwfw/xsxkapp/public/js/xsxkpub.js"></script>'
            "</body></html>"
        )
        client, _ = make_client(FakeResponse(None, status_code=200, text=app_shell))
        with pytest.raises(TokenExpired, match="登录态已失效"):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_302_跳首页视为登录失效(self):
        """实测：带着首页 cookie 请求时，未鉴权会返回 302 → *default/index.do。"""
        resp = FakeResponse(
            None,
            status_code=302,
            text="",
            headers={"Location": "http://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/*default/index.do"},
        )
        client, _ = make_client(resp)
        with pytest.raises(TokenExpired, match="重定向到首页"):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_跟随重定向落回首页也视为登录失效(self):
        """requests 自动跟随 302 后，落点是首页 HTML，同样要识别出来。"""
        resp = FakeResponse(
            None,
            status_code=200,
            text='<!DOCTYPE html><html><title>选课</title><script src="/xsxkapp/x.js">',
            url="http://xk.bit.edu.cn/xsxkapp/sys/xsxkapp/*default/index.do",
        )
        client, _ = make_client(resp)
        with pytest.raises(TokenExpired):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_意外重定向抛_api_错误而非登录失效(self):
        """不是所有重定向都是登录失效，别把异常情况误报成过期。"""
        resp = FakeResponse(
            None, status_code=302, text="", headers={"Location": "https://example.com/x"}
        )
        client, _ = make_client(resp)
        with pytest.raises(ApiError, match="意外重定向"):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_429_视为限流(self):
        client, _ = make_client(FakeResponse(None, status_code=429, text=""))
        with pytest.raises(RateLimited):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_code为302的json_也视为登录失效(self):
        """前端另有一条 code=='302' 的 JSON 分支，两条路径都要处理。"""
        client, _ = make_client(envelope(code="302", msg=""))
        with pytest.raises(TokenExpired):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_纯垃圾内容抛_api_错误(self):
        client, _ = make_client(FakeResponse(None, text="<<<garbage>>>"))
        with pytest.raises(ApiError, match="非 JSON"):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_网络异常向上抛出(self):
        client, _ = make_client(NetworkError("连接超时"))
        with pytest.raises(NetworkError):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_批次错误抛出_not_in_batch(self):
        client, _ = make_client(envelope(code="-1", msg="当前批次不可用"))
        with pytest.raises(NotInBatchError):
            client.query_courses("x", batch_code="B", student_code="S")

    def test_业务错误含登录二字不算登录失效(self):
        """「单点登录用户登记失败」含"登录"却是业务错误，误判会导致死循环重登。"""
        from bitxk.client import is_auth_failure

        assert not is_auth_failure("单点登录用户登记失败")
        assert is_auth_failure("请重新登录")
        assert is_auth_failure("登录已超时")
        assert is_auth_failure("token 无效")

    def test_code按字符串比较而非数字(self):
        """信封里的 code 是字符串 '1'，不能和整数 1 比较。"""
        client, _ = make_client(envelope(code="1", dataList=[]))
        assert client.query_courses("x", batch_code="B", student_code="S") == []


# --------------------------------------------------------------------------
# 学生信息与批次
# --------------------------------------------------------------------------


class TestBatches:
    def test_批次来自_student_学号端点(self):
        payload = envelope(
            data={
                "name": "张三",
                "number": "1120200001",
                "campus": "1",
                "electiveBatchList": [],
                "expElectiveBatchList": [],
            }
        )
        client, http = make_client(payload)
        client.student_info("1120200001")
        assert http.calls[0]["url"].endswith("/student/1120200001.do")

    def test_自动读取校区码(self):
        """campus 不是固定常量，必须从学生信息里取。"""
        payload = envelope(data={"campus": "7", "electiveBatchList": []})
        client, _ = make_client(payload)
        assert client.campus == ""
        client.student_info("S")
        assert client.campus == "7"

    def test_bind_可手工绑定(self):
        client, http = make_client()
        client.bind(student_code="S9", campus="3")
        assert client.campus == "3"
        assert http.student_code == "S9"

    def test_取当前可选批次(self):
        payload = envelope(
            data={
                "campus": "2",
                "electiveBatchList": [
                    {"code": "OLD", "canSelect": "0", "name": "已结束"},
                    {"code": "NOW", "canSelect": "1", "name": "第一轮"},
                ],
            }
        )
        client, _ = make_client(payload)
        batch = client.current_batch("S")
        assert batch.code == "NOW"
        assert batch.can_select

    def test_实验课批次也被纳入(self):
        payload = envelope(
            data={
                "electiveBatchList": [],
                "expElectiveBatchList": [{"code": "EXP", "canSelect": "1", "name": "实验课轮次"}],
            }
        )
        client, _ = make_client(payload)
        assert client.current_batch("S").code == "EXP"

    def test_需要确认但未确认的批次被跳过(self):
        """前端要求 needConfirm 已确认才能选，未确认的批次提交必被拒。"""
        payload = envelope(
            data={
                "electiveBatchList": [
                    {"code": "NEED", "canSelect": "1", "needConfirm": "1", "isConfirmed": "0"},
                    {"code": "OK", "canSelect": "1"},
                ],
            }
        )
        client, _ = make_client(payload)
        assert client.current_batch("S").code == "OK"

    def test_没有可选批次时报错并列出已知批次(self):
        payload = envelope(
            data={"electiveBatchList": [{"code": "A", "canSelect": "0", "name": "已结束"}]}
        )
        client, _ = make_client(payload)
        with pytest.raises(NotInBatchError, match="不在可选课时间"):
            client.current_batch("S")

    def test_学生信息无data时报错(self):
        """register/student 接口失败时 code=0 + msg，必须给出可读报错。"""
        client, _ = make_client(envelope(code="0", msg="单点登录用户登记失败"))
        with pytest.raises(ApiError, match="单点登录用户登记失败"):
            client.student_info("S")


# --------------------------------------------------------------------------
# 选课提交（异步）
# --------------------------------------------------------------------------


class TestSubmitAsync:
    """选课是异步的：受理 != 成功。以下是本模块最关键的测试。"""

    def test_受理后轮询并确认成功(self):
        client, http = make_client(
            envelope(code="1", msg=""),  # volunteer.do 受理
            envelope(code="1", msg="添加选课成功"),  # studentstatus.do 成功
        )
        result = client.submit("1001", batch_code="B", student_code="S", course_name="课")
        assert result.outcome is SelectionOutcome.SUCCESS
        assert result.ok
        assert "studentstatus.do" in http.calls[1]["url"]

    def test_受理不等于成功(self):
        """回归防线：绝不能把 volunteer.do 的 code=='1' 直接当成选课成功。"""
        client, http = make_client(
            envelope(code="1", msg=""),  # 受理
            envelope(code="0", msg=""),  # 仍在处理
            envelope(code="0", msg=""),  # 仍在处理
            envelope(code="-1", msg="选课时发生错误，超过限选人数。"),  # 终态：失败
            process_poll_attempts=3,
        )
        result = client.submit("1001", batch_code="B", student_code="S")
        assert len(http.calls) == 4  # 1 受理 + 3 轮询
        # 最终应反映真实失败，而不是"成功"
        assert result.outcome is SelectionOutcome.FULL

    def test_轮询得到失败原因(self):
        client, _ = make_client(
            envelope(code="1"),
            envelope(code="-1", msg="该课程与学生已选课程存在时间冲突。"),
            process_poll_attempts=1,
        )
        result = client.submit("1001", batch_code="B", student_code="S")
        assert result.outcome is SelectionOutcome.CONFLICT

    def test_轮询超时视为结果未知而非失败(self):
        """后台可能仍在处理，报"失败"会误导用户重复尝试。"""
        responses = [envelope(code="1")] + [envelope(code="0", msg="")] * 3
        client, _ = make_client(*responses, process_poll_attempts=3)
        result = client.submit("1001", batch_code="B", student_code="S")
        assert result.outcome is SelectionOutcome.PENDING
        assert not result.ok
        assert not result.outcome.should_retry

    def test_可跳过等待只要受理结果(self):
        client, http = make_client(envelope(code="1", msg=""))
        result = client.submit("1001", batch_code="B", student_code="S", wait_for_result=False)
        assert result.outcome is SelectionOutcome.ACCEPTED
        assert len(http.calls) == 1  # 没有发轮询请求

    def test_提交参数正确(self):
        client, http = make_client(envelope(code="1"), envelope(code="1", msg="成功"))
        client.submit(
            "1001",
            batch_code="B",
            student_code="S",
            teaching_class_type=CourseType.XGXK,
            course_name="科幻文学",
        )
        add_param = json.loads(form_body(http.calls[0])["addParam"])
        # 请求里是 teachingClassId（驼峰 Id），响应里才是 teachingClassID
        assert add_param["data"]["teachingClassId"] == "1001"
        assert add_param["data"]["operationType"] == "1"
        assert add_param["data"]["teachingClassType"] == "XGXK"

    def test_受理失败不抛异常而是返回结果(self):
        """满员是轮询中的常态，不能当异常处理。"""
        client, _ = make_client(envelope(code="0", msg="超过限选人数"))
        result = client.submit("1001", batch_code="B", student_code="S")
        assert not result.ok
        assert result.outcome is SelectionOutcome.FULL
        assert result.outcome.should_retry

    def test_受理阶段直接被拒也只返回结果(self):
        """code=0 表示请求被直接拒绝，不该抛异常、也不该去轮询。"""
        client, http = make_client(envelope(code="0", msg="不在选课时间内"))
        result = client.submit("1001", batch_code="B", student_code="S")
        assert not result.ok
        assert result.outcome is SelectionOutcome.NOT_IN_BATCH
        assert len(http.calls) == 1  # 没有多余的轮询请求

    def test_未预期的code仍走轮询确认(self):
        """code 语义不明确时宁可多问一次，也不要误报失败让用户重复尝试。"""
        client, _ = make_client(
            envelope(code="9", msg="系统忙"),
            envelope(code="1", msg="添加选课成功"),
            process_poll_attempts=2,
        )
        result = client.submit("1001", batch_code="B", student_code="S")
        assert result.outcome is SelectionOutcome.SUCCESS

    def test_登录失效仍然抛异常(self):
        """这类问题必须让上层感知以便重登，不能降级成结果对象。"""
        client, _ = make_client(token_expired_response())
        with pytest.raises(TokenExpired):
            client.submit("1001", batch_code="B", student_code="S")

    def test_限流仍然抛异常(self):
        client, _ = make_client(FakeResponse(None, status_code=429, text=""))
        with pytest.raises(RateLimited):
            client.submit("1001", batch_code="B", student_code="S")


class TestWithdraw:
    def test_退选用deleteParam与operationType2(self):
        client, http = make_client(envelope(code="1"), envelope(code="1", msg="退选成功"))
        client.withdraw("1001", batch_code="B", student_code="S")
        body = form_body(http.calls[0])
        assert "deleteParam" in body
        assert "addParam" not in body
        param = json.loads(body["deleteParam"])
        assert param["data"]["operationType"] == "2"
        # 退选不带 campus / teachingClassType
        assert "campus" not in param["data"]
        assert "teachingClassType" not in param["data"]


class TestCanChoose:
    def test_返回不可选原因(self):
        client, http = make_client(envelope(data={"reasonList": ["学分已满", "冲突"]}))
        reasons = client.can_choose("1001", batch_code="B", student_code="S")
        assert reasons == ["学分已满", "冲突"]
        assert http.calls[0]["url"].endswith("/util/canchoose.do")

    def test_可选的返回空列表(self):
        client, _ = make_client(envelope(data={"reasonList": []}))
        assert client.can_choose("1001", batch_code="B", student_code="S") == []

    @pytest.mark.parametrize(
        "exc",
        [ApiError("接口返回错误"), TokenExpired("失效"), RateLimited("限流")],
    )
    def test_接口异常时静默返回空(self, exc):
        """这是辅助判断，失败不该影响主流程。"""
        client, _ = make_client(exc)
        assert client.can_choose("1001", batch_code="B", student_code="S") == []


# --------------------------------------------------------------------------
# 教学班筛选
# --------------------------------------------------------------------------


class TestFindTeachingClasses:
    def test_精确匹配过滤掉近似课程名(self):
        payload = envelope(
            dataList=[
                {
                    "courseName": "大学语文",
                    "teachingClassID": "1",
                    "classCapacity": "40",
                    "numberOfFirstVolunteer": "1",
                },
                {
                    "courseName": "大学语文（进阶）",
                    "teachingClassID": "2",
                    "classCapacity": "40",
                    "numberOfFirstVolunteer": "1",
                },
            ]
        )
        client, _ = make_client(payload)
        found = client.find_teaching_classes("大学语文", batch_code="B", student_code="S")
        assert [tc.teaching_class_id for tc in found] == ["1"]

    def test_关闭精确匹配时全部返回(self):
        payload = envelope(
            dataList=[
                {"courseName": "大学语文", "teachingClassID": "1"},
                {"courseName": "大学语文（进阶）", "teachingClassID": "2"},
            ]
        )
        client, _ = make_client(payload)
        found = client.find_teaching_classes(
            "大学语文", batch_code="B", student_code="S", exact_match=False
        )
        assert len(found) == 2


# --------------------------------------------------------------------------
# 语义分类
# --------------------------------------------------------------------------


class TestClassifyError:
    @pytest.mark.parametrize(
        ("msg", "expected"),
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

    def test_冲突优先于已选(self):
        """「该课程与学生已选课程存在时间冲突」同时含"已选"和"冲突"，
        必须归类为冲突，否则会被当成已选而误判为完成。"""
        assert _classify_error("该课程与学生已选课程存在时间冲突。") is SelectionOutcome.CONFLICT

    def test_限选人数优先于已选(self):
        """「超过限选人数」含"选"字，必须先判容量。"""
        assert _classify_error("选课时发生错误，超过限选人数。") is SelectionOutcome.FULL


class TestCourseType:
    def test_八个类型齐全(self):
        assert set(CourseType.ALL) == {
            "TJKC",
            "FANKC",
            "FAWKC",
            "XGXK",
            "CXKC",
            "TYKC",
            "FXKC",
            "QXKC",
        }

    def test_中文标签(self):
        assert CourseType.label("XGXK") == "校公选课"
        assert CourseType.label("TYKC") == "体育课程"
        assert CourseType.label("FAWKC") == "方案外课程"

    def test_未知类型回退为原文(self):
        assert CourseType.label("ZZZZ") == "ZZZZ"
        assert CourseType.endpoint("ZZZZ") == CourseType.DEFAULT_ENDPOINT
