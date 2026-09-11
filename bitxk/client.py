"""选课系统（wisedu xsxkapp）业务接口客户端。

接口调用约定
------------
参数以 **POST 表单体**发送，值是一个 JSON 字符串，键名固定::

    POST /xsxkapp/sys/xsxkapp/elective/publicCourse.do
    Content-Type: application/x-www-form-urlencoded
    token: <全小写>
    language: zh_cn

    querySetting={"data":{...},"pageSize":"10","pageNumber":"0","order":""}

鉴权靠全小写 header ``token``；token 失效返回 **HTTP 401 + text/html
"Not login!"**（不是 JSON、不是 302），因此必须先判状态码再解析正文。

**选课是异步的**：``elective/volunteer.do`` 返回 ``code=='1'`` 仅表示"已受理"，
真正的成败要轮询 ``elective/studentstatus.do``（``'1'`` 成功 / ``'-1'`` 失败 /
其它继续等）。本模块的 :meth:`XkClient.submit` 默认会完成整个确认流程。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from typing import Any

from .auth import API_BASE
from .exceptions import (
    ApiError,
    NetworkError,
    NotInBatchError,
    RateLimited,
    ServerBusy,
    TokenExpired,
)
from .http import HttpClient
from .models import (
    Batch,
    Course,
    SelectionOutcome,
    SelectionResult,
    TeachingClass,
    _as_bool,
    _pick,
)

logger = logging.getLogger(__name__)

__all__ = ["CourseType", "XkClient"]

#: 响应信封里代表成功的 code。
#: 信封文档：``"1"`` 成功 / ``"0"`` 业务失败 / ``"-1"`` 处理失败 /
#: ``"2"|"3"|"4"`` 登录相关 / ``"302"`` 登录态失效。
#: **注意 code 是字符串**，比较前一律 ``str()``。
_CODE_SUCCESS = {"1"}

#: 允许与服务端历史版本兼容的等价成功码（当前版本未观察到，保留兜底）。
_CODE_SUCCESS_ALIASES = {"200"}

#: 提交选课时表示「请求被直接拒绝、无需轮询确认」的 code。
#: 实测前端逻辑：``volunteer.do`` 返回 ``code=='1'`` 表示已受理（异步），
#: 其余非 302 的值都当作直接失败并把 ``msg`` 弹给用户。
_CODE_IMMEDIATE_FAILURE = {"0"}

#: 判定「成功」时统一用这个集合
_ALL_SUCCESS_CODES = _CODE_SUCCESS | _CODE_SUCCESS_ALIASES

_CODE_BATCH_ERROR = {"-1", "3", "5"}

#: 在线人数超过上限。前端文案：「在线人数超过上限，请稍后再试！」
_CODE_SERVER_BUSY = {"4"}


class CourseType:
    """``teachingClassType`` 枚举 —— 决定查哪个池子的课。

    取值与端点路由均来自生产前端 ``grablessons.js`` 的源码，非推测。
    注意 **体育课没有独立端点**：它与方案内/方案外/重修/辅修共用
    ``programCourse.do``，由服务端按 body 里的 ``teachingClassType`` 分流。
    """

    TJKC = "TJKC"  # 推荐课程      -> recommendedCourse.do
    FANKC = "FANKC"  # 方案内课程    -> programCourse.do
    FAWKC = "FAWKC"  # 方案外课程    -> programCourse.do
    XGXK = "XGXK"  # 校公选课      -> publicCourse.do
    CXKC = "CXKC"  # 重修课程      -> programCourse.do
    TYKC = "TYKC"  # 体育课程      -> programCourse.do
    FXKC = "FXKC"  # 辅修课程      -> programCourse.do（唯一 isMajor='0'）
    QXKC = "QXKC"  # 全校课程      -> elective/course.do

    ALL = [TJKC, FANKC, FAWKC, XGXK, CXKC, TYKC, FXKC, QXKC]

    LABELS = {
        TJKC: "推荐课程",
        FANKC: "方案内课程",
        FAWKC: "方案外课程",
        XGXK: "校公选课",
        CXKC: "重修课程",
        TYKC: "体育课程",
        FXKC: "辅修课程",
        QXKC: "全校课程",
    }

    #: 各类型对应的查询端点
    ENDPOINTS = {
        TJKC: "elective/recommendedCourse.do",
        XGXK: "elective/publicCourse.do",
        QXKC: "elective/course.do",
    }
    #: 其余类型共用的端点
    DEFAULT_ENDPOINT = "elective/programCourse.do"

    @classmethod
    def label(cls, code: str) -> str:
        return cls.LABELS.get(code, code)

    @classmethod
    def endpoint(cls, code: str) -> str:
        return cls.ENDPOINTS.get(code, cls.DEFAULT_ENDPOINT)

    @classmethod
    def is_major(cls, code: str) -> str:
        """``isMajor`` 取值 —— 只有辅修课程是 ``'0'``。"""
        return "0" if code == cls.FXKC else "1"


class XkClient:
    """选课系统的业务封装。所有方法失败时抛出 :mod:`bitxk.exceptions` 里的异常。

    与接口交互的三条关键约定（均来自生产前端源码）：

    1. 参数以 **POST 表单体**发送，值为一个 JSON 字符串，键名是
       ``querySetting`` / ``addParam`` / ``deleteParam``；
    2. 鉴权靠全小写 header ``token``（另发 ``language``）；
    3. **选课是异步的** —— ``volunteer.do`` 返回 ``code=='1'`` 只代表"已受理"，
       最终结果必须轮询 ``elective/studentstatus.do``。
    """

    def __init__(
        self,
        http: HttpClient,
        *,
        api_base: str = API_BASE,
        campus: str = "",
        language: str = "zh_cn",
        process_poll_interval: float = 1.0,
        process_poll_attempts: int = 10,
    ) -> None:
        self.http = http
        self.api_base = api_base.rstrip("/")
        #: 校区码。**不是固定常量**，取自学生信息；未探测到时留空由服务端兜底。
        self.campus = campus
        self.language = language
        #: 提交后轮询处理结果的节奏，对齐前端（1 秒 × 10 次）
        self.process_poll_interval = process_poll_interval
        self.process_poll_attempts = process_poll_attempts

    # ================================================================ 基础

    def _url(self, path: str) -> str:
        return f"{self.api_base}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        """鉴权头。header 名是全小写 ``token``（与生产前端一致）。"""
        headers = {"language": self.language}
        token = getattr(self.http, "token", None)
        if token:
            headers["token"] = token
        return headers

    def bind(self, *, student_code: str = "", campus: str = "") -> None:
        """绑定学号与校区码。

        两者都来自 ``student/<学号>.do``，**不是常量**：
        查询接口必须带上正确的 ``campus``，写死会把别的校区的课查漏。
        """
        if student_code:
            self.http.student_code = student_code  # type: ignore[attr-defined]
        if campus:
            self.campus = campus

    def _post(
        self,
        path: str,
        data: dict[str, str],
        *,
        raise_on_business_error: bool = True,
    ) -> Any:
        """发一次 POST 业务请求并返回解析后的 JSON 信封。

        参数一律放 **POST 表单体**（与生产前端一致）；不放 URL query。
        """
        return self._request(
            "POST",
            path,
            headers=self._headers(),
            data=data,
            raise_on_business_error=raise_on_business_error,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        raise_on_business_error: bool = True,
        **kwargs: Any,
    ) -> Any:
        """发一次业务请求并解析响应信封（GET/POST 共用）。"""
        url = self._url(path)
        resp = self.http.request(method, url, **kwargs)

        # token 失效有三种实测形态，必须**先判状态码再解析正文**，
        # 否则 resp.json() 会直接抛异常、连原因都看不到：
        #   1) HTTP 302 → Location 指向 *default/index.do（带 cookie 时最常见）；
        #   2) HTTP 401 + text/html "Not login!"（无 cookie 时的网关响应）；
        #   3) HTTP 200 + 应用首页 HTML（requests 自动跟完 302 后的落点）。
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "") or str(getattr(resp, "url", ""))
            if _looks_like_login_page(location) or "index.do" in location:
                raise TokenExpired(f"接口 {path} 重定向到首页，登录态已失效")
            raise ApiError(f"接口 {path} 发生意外重定向：{location[:120]}")
        if resp.status_code in (401, 403):
            raise TokenExpired(f"登录态失效（HTTP {resp.status_code}）")
        if resp.status_code == 429:
            raise RateLimited("选课系统限流")

        try:
            payload = resp.json()
        except ValueError as exc:
            # 跟随重定向后落到首页，也会走到这里
            if _looks_like_login_page(resp.text or "") or _redirected_to_index(resp):
                raise TokenExpired("选课系统返回了页面而非数据，登录态已失效") from exc
            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise ApiError(f"接口 {path} 返回了非 JSON 内容：{snippet}", payload=snippet) from exc

        if not isinstance(payload, dict):
            raise ApiError(f"接口 {path} 返回结构异常：{type(payload).__name__}")

        # 选课接口需要自己解释业务 code（"受理成功"与"直接失败"由调用方区分），
        # 所以允许关掉这里的自动抛错。
        if raise_on_business_error:
            self._raise_for_code(path, payload)
        return payload

    @staticmethod
    def _raise_for_code(path: str, payload: dict) -> None:
        """根据信封 code 抛出语义化异常。``code`` 是**字符串**，比较前务必转 str。"""
        code = payload.get("code")
        if code is None:
            return
        code_str = str(code)
        if code_str in _CODE_SUCCESS or code_str in _CODE_SUCCESS_ALIASES:
            return

        msg = str(payload.get("msg") or payload.get("message") or "")

        if is_auth_failure(msg, code_str):
            raise TokenExpired(f"接口 {path} 要求重新登录：{msg or code_str}")
        if "频繁" in msg or "限制" in msg:
            raise RateLimited(f"接口 {path} 被限流：{msg}")
        if code_str in _CODE_SERVER_BUSY:
            raise ServerBusy(msg or "选课系统在线人数已达上限，请稍后再试")
        if code_str in _CODE_BATCH_ERROR and any(word in msg for word in ("批次", "轮次", "阶段")):
            raise NotInBatchError(msg or f"接口 {path} 批次不可用")

        raise ApiError(f"接口 {path} 返回错误：code={code} msg={msg}", code=code, payload=payload)

    # ================================================================ 学生 / 批次

    def student_info(self, student_code: str = "") -> dict:
        """取学生信息（含批次列表、校区码）。

        批次 ``electiveBatchList`` 的真源是 ``student/{学号}.do`` ——
        ``student/xkxf.do`` 只返回学分统计且前端已停用；
        ``elective/studentstatus.do`` 返回的是提交处理状态，与批次无关。
        """
        code = student_code or getattr(self.http, "student_code", "") or ""
        if not code:
            raise ApiError("获取学生信息需要学号（studentInfo 端点形如 student/<学号>.do）")

        # 前端是 **GET** student/<学号>.do?timestamp=<ms>，不是 POST。
        # 用 GET 才能与生产行为一致（POST 在部分网关配置下会被拒）。
        payload = self._request(
            "GET",
            f"student/{code}.do",
            # 鉴权头不能漏：漏了会拿到首页 HTML，表现成"登录态失效"
            headers=self._headers(),
            params={"timestamp": str(int(time.time() * 1000))},
        )

        # 有学籍信息时，data.code 就是学号；为空说明该账号没有学籍
        info = payload.get("data")
        if not isinstance(info, dict) or not info:
            raise ApiError(f"学生信息接口未返回 data：{payload.get('msg') or payload}")

        number = _pick(info, "code", "number", "xh")
        if number in (None, ""):
            raise ApiError(
                "登录的账户未查询到学籍信息。请确认使用的是本科生账号，"
                "并已在统一身份认证中完成登录。"
            )

        # 注意：campus **不是**学生信息里的字段（前端是按每个课程行取
        # data.campus 并回填到按钮上的）。这里只在接口确实给出时顺带记录，
        # 拿不到就保持调用方传入的值（默认空，由服务端兜底）。
        campus = _pick(info, "campus", "campusCode")
        if campus not in (None, ""):
            self.campus = str(campus).strip()
        return info

    def batches(self, student_code: str = "") -> list[Batch]:
        """取全部选课批次（常规轮次 + 实验课轮次）。"""
        info = self.student_info(student_code)
        raw: list = []
        for key in ("electiveBatchList", "expElectiveBatchList"):
            value = _pick(info, key) or []
            if isinstance(value, list):
                raw.extend(value)
        return [Batch.from_api(item) for item in raw if isinstance(item, dict)]

    def current_batch(self, student_code: str = "") -> Batch:
        """取当前可选批次；没有任何可选批次时抛 :class:`NotInBatchError`。

        前端还要求 ``needConfirm`` 已确认；未确认的批次不能提交，
        这里同样把它排除掉，避免白跑一轮。
        """
        all_batches = self.batches(student_code)
        for batch in all_batches:
            if not batch.can_select:
                continue
            if _as_bool(batch.raw.get("needConfirm")) and not _as_bool(
                batch.raw.get("isConfirmed")
            ):
                logger.debug("批次 %s 需要确认但尚未确认，跳过", batch.code)
                continue
            return batch

        detail = "；".join(str(b) for b in all_batches) or "（接口未返回任何批次）"
        raise NotInBatchError(f"当前不在可选课时间内。已知批次：{detail}")

    # ================================================================ 课程查询

    def query_courses(
        self,
        keyword: str,
        *,
        teaching_class_type: str = CourseType.XGXK,
        batch_code: str = "",
        student_code: str = "",
        page_size: int = 10,
        page_number: int = 0,
        order: str = "",
        check_conflict: str = "0",
        check_capacity: str = "2",
    ) -> list[Course]:
        """按关键词查询课程。

        Args:
            keyword: 课程名关键词。
            teaching_class_type: 见 :class:`CourseType`。
            batch_code: 批次号，来自 :meth:`current_batch`。
            student_code: 学号。
            page_size: 每页条数。前端固定 10；轮询时适当放大可减少请求数。
            check_capacity: ``'0'`` 不校验 / ``'1'`` 校验并**过滤掉**满员课 /
                ``'2'`` 校验但不**过滤**（仅标注）。
                **轮询必须用 ``'2'``** —— 用 ``'1'`` 时满员课直接从列表消失，
                就永远观察不到「已满 → 有余量」的翻转。
            check_conflict: 同上，``'0'`` 不校验，避免把冲突课滤掉导致误判。
        """
        code = student_code or getattr(self.http, "student_code", "") or ""
        if not code:
            raise ApiError("查询课程需要学号")

        data = {
            "studentCode": code,
            "campus": self.campus,
            "electiveBatchCode": batch_code,
            "isMajor": CourseType.is_major(teaching_class_type),
            "teachingClassType": teaching_class_type,
            "checkConflict": check_conflict,
            "checkCapacity": check_capacity,
            "queryContent": keyword,
        }
        setting = json.dumps(
            {
                "data": data,
                "pageSize": str(page_size),
                "pageNumber": str(page_number),
                "order": order,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        path = CourseType.endpoint(teaching_class_type)
        payload = self._post(path, {"querySetting": setting})

        # dataList 与 data 平级，不在 data 里面
        items = _pick(payload, "dataList", "list", "rows") or []
        if isinstance(items, dict):
            items = _pick(items, "dataList", "list", "rows") or []
        courses = [
            Course.from_api(item, teaching_class_type=teaching_class_type)
            for item in items
            if isinstance(item, dict)
        ]
        logger.debug("查询 %s(%s) 命中 %d 门课", keyword, teaching_class_type, len(courses))
        return courses

    def find_teaching_classes(
        self,
        keyword: str,
        *,
        teaching_class_type: str = CourseType.XGXK,
        batch_code: str = "",
        student_code: str = "",
        exact_match: bool = True,
        page_size: int = 30,
    ) -> list[TeachingClass]:
        """按课程名找到所有候选教学班。

        ``exact_match=True`` 时只保留课程名与关键词完全一致的课
        （避免「大学语文」误匹配「大学语文（进阶）」）。

        另外会**过滤掉前端标记为不可操作的行**（``inQuene`` 队列处理中等），
        它们提交必被拒，留着只会浪费请求配额。
        """
        courses = self.query_courses(
            keyword,
            teaching_class_type=teaching_class_type,
            batch_code=batch_code,
            student_code=student_code,
            page_size=page_size,
            check_capacity="2",
            check_conflict="0",
        )

        wanted = keyword.strip()
        result: list[TeachingClass] = []
        for course in courses:
            if exact_match and course.name.strip() and course.name.strip() != wanted:
                continue
            result.extend(course.teaching_classes)
        return result

    # ================================================================ 选课提交

    def submit(
        self,
        teaching_class_id: str,
        *,
        batch_code: str,
        student_code: str,
        teaching_class_type: str = CourseType.XGXK,
        course_name: str = "",
        wait_for_result: bool = True,
    ) -> SelectionResult:
        """提交选课并（可选）等待异步处理结果。

        这套系统的选课是**异步**的：``volunteer.do`` 返回 ``code=='1'``
        只代表"请求已受理、进入后台处理"，真正的成败要轮询
        ``elective/studentstatus.do``：

        * ``code=='1'``  → 选课成功
        * ``code=='-1'`` → 选课失败，真实原因在 ``msg``
        * 其它           → 仍在处理，1 秒后重试，最多 10 次

        因此本方法默认会完成整个"受理 + 确认"流程，
        返回的才是**真正的结果**。
        """
        code = student_code or getattr(self.http, "student_code", "") or ""
        add_param = json.dumps(
            {
                "data": {
                    "operationType": "1",
                    "studentCode": code,
                    "electiveBatchCode": batch_code,
                    # 注意：请求里是 teachingClassId（驼峰 Id），
                    # 响应里是 teachingClassID（大写 ID），别写错。
                    "teachingClassId": teaching_class_id,
                    "isMajor": CourseType.is_major(teaching_class_type),
                    "campus": self.campus,
                    "teachingClassType": teaching_class_type,
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        payload = self._post(
            "elective/volunteer.do",
            {"addParam": add_param},
            # 自己解释业务 code，因为 "0" 既可能是"受理失败"也可能是别的中间态
            raise_on_business_error=False,
        )

        accepted = str(payload.get("code", ""))
        accepted_msg = str(payload.get("msg") or "")

        if accepted == "302":
            raise TokenExpired("选课请求被拒绝：登录态失效（code=302）")

        if accepted in _ALL_SUCCESS_CODES:
            # 已受理 —— 这才是异步流程的起点
            if not wait_for_result:
                return SelectionResult(
                    outcome=SelectionOutcome.ACCEPTED,
                    message=accepted_msg or "已受理，等待系统处理",
                    code=payload.get("code"),
                    teaching_class_id=teaching_class_id,
                    course_name=course_name,
                    raw=payload,
                )
            return self.wait_for_result(
                teaching_class_id=teaching_class_id,
                student_code=code,
                course_name=course_name,
                accepted_message=accepted_msg,
            )

        if accepted in _CODE_IMMEDIATE_FAILURE:
            return SelectionResult(
                outcome=_classify_error(accepted_msg, accepted),
                message=accepted_msg or "选课请求被拒绝",
                code=payload.get("code"),
                teaching_class_id=teaching_class_id,
                course_name=course_name,
                raw=payload,
            )

        # 其余 code：语义不明确，仍交给轮询去确认，避免误判成失败
        if not wait_for_result:
            return SelectionResult(
                outcome=SelectionOutcome.ACCEPTED,
                message=accepted_msg or f"接口返回 code={accepted}，等待系统处理",
                code=payload.get("code"),
                teaching_class_id=teaching_class_id,
                course_name=course_name,
                raw=payload,
            )
        return self.wait_for_result(
            teaching_class_id=teaching_class_id,
            student_code=code,
            course_name=course_name,
            accepted_message=accepted_msg,
        )

    def wait_for_result(
        self,
        *,
        teaching_class_id: str,
        student_code: str,
        course_name: str = "",
        accepted_message: str = "",
    ) -> SelectionResult:
        """轮询 ``elective/studentstatus.do`` 直到拿到终态。

        对齐前端行为：1 秒一次，最多 10 次；超时按"结果未知"处理
        （**不是失败** —— 后台可能仍在处理，下次轮询课程列表即可确认）。
        """
        last_msg = accepted_message
        for attempt in range(self.process_poll_attempts):
            if attempt:
                time.sleep(self.process_poll_interval)
            try:
                payload = self._post(
                    "elective/studentstatus.do",
                    {"studentCode": student_code},
                    # 必须自己解释 code：终态 `-1` 就是"选课失败，原因在 msg"，
                    # 若让 _raise_for_code 把它转成异常，真实原因会被吞掉，
                    # 最终只会得到一个没有信息量的"结果未知"。
                    raise_on_business_error=False,
                )
            except NetworkError as exc:
                logger.debug("查询处理状态网络异常（第 %d 次）：%s", attempt + 1, exc)
                continue
            except ApiError as exc:
                logger.debug("查询处理状态失败（第 %d 次）：%s", attempt + 1, exc)
                continue

            code = str(payload.get("code", ""))
            msg = str(payload.get("msg") or "")

            if code == "1":
                return SelectionResult(
                    outcome=SelectionOutcome.SUCCESS,
                    message=msg or "添加选课成功",
                    code=code,
                    teaching_class_id=teaching_class_id,
                    course_name=course_name,
                    raw=payload,
                )
            if code == "-1":
                return SelectionResult(
                    outcome=_classify_error(msg, code),
                    message=msg or "选课失败",
                    code=code,
                    teaching_class_id=teaching_class_id,
                    course_name=course_name,
                    raw=payload,
                )

            last_msg = msg or last_msg
            logger.debug("选课处理中（第 %d 次），code=%s", attempt + 1, code)

        return SelectionResult(
            outcome=SelectionOutcome.PENDING,
            message=last_msg or "选课请求已提交，但未在预期时间内拿到处理结果",
            teaching_class_id=teaching_class_id,
            course_name=course_name,
        )

    def withdraw(
        self,
        teaching_class_id: str,
        *,
        batch_code: str,
        student_code: str,
        course_name: str = "",
    ) -> SelectionResult:
        """退选。

        与选课**同一个端点**，参数名换成 ``deleteParam``、``operationType='2'``，
        且不带 ``campus`` / ``teachingClassType`` / ``needBook``。
        """
        code = student_code or getattr(self.http, "student_code", "") or ""
        delete_param = json.dumps(
            {
                "data": {
                    "operationType": "2",
                    "studentCode": code,
                    "electiveBatchCode": batch_code,
                    "teachingClassId": teaching_class_id,
                    "isMajor": "1",
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        try:
            payload = self._post("elective/volunteer.do", {"deleteParam": delete_param})
        except (TokenExpired, RateLimited):
            raise
        except ApiError as exc:
            return SelectionResult(
                outcome=_classify_error(str(exc), exc.code),
                message=str(exc),
                code=exc.code,
                teaching_class_id=teaching_class_id,
                course_name=course_name,
            )

        if str(payload.get("code", "")) in _ALL_SUCCESS_CODES:
            return self.wait_for_result(
                teaching_class_id=teaching_class_id,
                student_code=code,
                course_name=course_name,
                accepted_message=str(payload.get("msg") or "退选请求已受理"),
            )
        return SelectionResult(
            outcome=_classify_error(str(payload.get("msg") or ""), payload.get("code")),
            message=str(payload.get("msg") or ""),
            code=payload.get("code"),
            teaching_class_id=teaching_class_id,
            course_name=course_name,
            raw=payload,
        )

    # ================================================================ 已选课程

    def can_choose(
        self, teaching_class_id: str, *, batch_code: str, student_code: str
    ) -> list[str]:
        """问服务端「这个教学班我能不能选」，返回不可选原因列表（空表示可以）。

        这是 ``util/canchoose.do``，服务端权威判定，比本地推断可靠。
        注意该端点无 token 时返回 **302** 而非 401。
        """
        code = student_code or getattr(self.http, "student_code", "") or ""
        setting = json.dumps(
            {
                "data": {
                    "studentCode": code,
                    "electiveBatchCode": batch_code,
                    "teachingClassId": teaching_class_id,
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            payload = self._post("util/canchoose.do", {"querySetting": setting})
        except (ApiError, TokenExpired, RateLimited):
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        reasons = _pick(data, "reasonList", "reason") or []
        if isinstance(reasons, list):
            return [str(r) for r in reasons if str(r).strip()]
        return [str(reasons)] if reasons else []

    def selected_courses(self, *, batch_code: str, student_code: str) -> list[dict]:
        """取本批次已选课程（用于确认是否真的选上了）。"""
        code = student_code or getattr(self.http, "student_code", "") or ""
        setting = json.dumps(
            {
                "data": {
                    "studentCode": code,
                    "campus": self.campus,
                    "electiveBatchCode": batch_code,
                    "isMajor": "1",
                    "teachingClassType": "",
                    "queryContent": "",
                },
                "pageSize": "200",
                "pageNumber": "0",
                "order": "",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for path in ("elective/course.do", "elective/queryCourse.do"):
            try:
                payload = self._post(path, {"querySetting": setting})
            except (ApiError, NotInBatchError):
                continue
            items = _pick(payload, "dataList", "list", "rows") or []
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []


# --------------------------------------------------------------------------
# 响应语义判定
# --------------------------------------------------------------------------


def _classify_error(msg: str, code: Any = None) -> SelectionOutcome:
    """根据错误文本判定结果类型。

    中文提示不稳定，所以用「关键词命中」而不是精确匹配，
    并让「容量已满」保持为可重试状态。

    注意判定顺序 —— 中文提示经常互相包含，顺序错了会误判：
    例如「该课程与学生**已选**课程存在时间**冲突**」同时命中「已选」和「冲突」，
    必须让更具体的语义优先。
    """
    text = (msg or "").strip()

    if any(word in text for word in ("成功", "已选中", "选课完成")):
        return SelectionOutcome.SUCCESS

    # 冲突必须先判：这类提示常带「已选课程」字样
    if "冲突" in text:
        return SelectionOutcome.CONFLICT

    # 容量类也要先于「已选」判定：「超过限选人数」含「选」字
    if any(
        word in text
        for word in (
            "超过限选",
            "限选人数",
            "人数已满",
            "已满",
            "名额",
            "容量不足",
            "没有余量",
            "余量不足",
        )
    ):
        return SelectionOutcome.FULL

    if any(word in text for word in ("已选", "重复", "已经选")):
        return SelectionOutcome.ALREADY

    if any(word in text for word in ("不在选课时间", "批次", "轮次", "阶段", "未开始", "已结束")):
        return SelectionOutcome.NOT_IN_BATCH

    # 「处理失败」类措辞必须先于其它规则判掉：
    # code=-1 的终态消息常形如「系统处理失败」，若落到下面的默认分支
    # 会得到一个没有信息量的 ERROR，用户看不到真实原因。
    if any(word in text for word in ("处理失败", "系统繁忙", "系统错误", "服务异常")):
        return SelectionOutcome.ERROR

    if any(word in text for word in ("频繁", "限制", "稍后")):
        return SelectionOutcome.RATE_LIMITED

    if any(word in text for word in ("登录", "认证", "超时", "token", "Token")):
        return SelectionOutcome.AUTH_ERROR

    if str(code) in _CODE_BATCH_ERROR:
        return SelectionOutcome.NOT_IN_BATCH
    return SelectionOutcome.ERROR


#: 真正表示「登录态失效」的措辞。
#: 不能简单匹配「登录」二字 —— 像「单点登录用户登记失败」这种业务错误
#: 也含「登录」，误判会导致无意义的重登循环。
_AUTH_FAILURE_PHRASES = (
    "重新登录",
    "请登录",
    "未登录",
    "登录已失效",
    "登录失效",
    "登录已过期",
    "登录已超时",
    "登录超时",
    "登录过期",
    "登录状态",
    "认证失败",
    "会话已失效",
)


def _redirected_to_index(resp) -> bool:
    """响应（或其跳转链）是否落到了选课系统首页 —— 即登录态失效。"""
    if "index.do" in str(getattr(resp, "url", "") or ""):
        return True
    for hop in getattr(resp, "history", None) or []:
        if "index.do" in str(hop.headers.get("Location", "")):
            return True
    return False


def is_auth_failure(msg: str, code: str = "") -> bool:
    """判断一段错误消息是否表示登录态失效。"""
    if str(code) == "302":
        return True
    if "token" in (msg or "").lower():
        return True
    return any(phrase in (msg or "") for phrase in _AUTH_FAILURE_PHRASES)


def _looks_like_login_page(text: str) -> bool:
    """判断一段非 JSON 响应是不是「未登录时的登录页 / 应用首页」。

    选课系统未登录时会返回 HTTP 200 + 一张 HTML 页面（有时是 SSO 登录页，
    有时直接是选课应用的首页外壳），因此必须靠内容特征识别，
    不能只看状态码。
    """
    if not text:
        return False
    head = text[:4000].lower()
    markers = (
        "<!doctype html",
        "<html",  # 是 HTML 而不是数据
        "sso.bit.edu.cn",
        "cas/login",
        "login-croypto",
        "xsxkapp",
        "xsxkpub",
        "选课",
    )
    return any(marker in head for marker in markers)


def flatten(courses: Iterable[Course]) -> list[TeachingClass]:
    """把课程列表摊平成教学班列表，便于统一轮询。"""
    out: list[TeachingClass] = []
    for course in courses:
        out.extend(course.teaching_classes)
    return out
