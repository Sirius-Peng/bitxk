"""选课系统（wisedu xsxkapp）业务接口客户端。

接口调用约定
------------
金智这套选课系统的参数传递方式比较特别：查询接口把 ``querySetting`` /
``addParam`` 这种「一个 JSON 字符串」直接塞进 URL 的 query string 里，
而不是放在 body。例如::

    POST /xsxkapp/sys/xsxkapp/elective/publicCourse.do
         ?querySetting={'data':{...},'pageSize':'10','pageNumber':'0','order':''}

本模块统一用 ``_call()`` 封装这个约定：优先按 query 参数发；如果服务端
返回参数解析类错误，则自动降级用 form body 重发一次（不同版本行为略有差异）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from .auth import API_BASE
from .exceptions import ApiError, NotInBatchError, RateLimited, TokenExpired
from .http import HttpClient
from .models import Batch, Course, SelectionOutcome, SelectionResult, TeachingClass, _pick

logger = logging.getLogger(__name__)

__all__ = ["CourseType", "XkClient"]

#: 选课系统返回的 code 语义（多版本兼容，不同批次可能只出现其中一部分）
_CODE_SUCCESS = {"1", "200", "0"}
_CODE_BATCH_ERROR = {"-1", "3", "5"}


class CourseType:
    """``teachingClassType`` 枚举 —— 决定查哪个池子的课。"""

    RECOMMENDED = "TJKC"  # 系统推荐课程
    PROGRAM = "FANKC"  # 培养方案内课程
    PUBLIC = "XGXK"  # 校公选课
    PE = "TYKC"  # 体育课程
    MAJOR = "XGKC"  # 专业课 / 跨专业（部分版本）
    GENERAL = "TJK"  # 通识（部分版本）

    #: 走 ``programCourse.do`` 查询的类型，其余走 ``publicCourse.do``
    PROGRAM_TYPES = {PROGRAM, PE, RECOMMENDED, MAJOR, GENERAL}
    ALL = [RECOMMENDED, PROGRAM, PUBLIC, PE, MAJOR, GENERAL]

    LABELS = {
        RECOMMENDED: "系统推荐",
        PROGRAM: "方案内课程",
        PUBLIC: "校公选课",
        PE: "体育课程",
        MAJOR: "专业课",
        GENERAL: "通识课",
    }

    @classmethod
    def label(cls, code: str) -> str:
        return cls.LABELS.get(code, code)


class XkClient:
    """选课系统的业务封装。所有方法失败时抛出 :mod:`bitxk.exceptions` 里的异常。"""

    def __init__(self, http: HttpClient, *, api_base: str = API_BASE, campus: str = "2") -> None:
        self.http = http
        self.api_base = api_base.rstrip("/")
        self.campus = campus

    # ================================================================ 基础

    def _url(self, path: str) -> str:
        return f"{self.api_base}/{path.lstrip('/')}"

    def _call(
        self,
        path: str,
        *,
        setting_key: str,
        setting: dict,
        extra_query: dict[str, str] | None = None,
        allow_form_fallback: bool = True,
    ) -> Any:
        """按 wisedu 的约定调用一个接口并返回解析后的 JSON。

        参数优先按 query string 传（当前版本的行为）。若服务端返回了
        非 JSON 内容（部分版本只认 form body），且 ``allow_form_fallback``
        为真，则自动改用 form body 重试一次。
        """
        payload = json.dumps(setting, ensure_ascii=False, separators=(",", ":"))
        url = self._url(path)
        query = {setting_key: payload}
        if extra_query:
            query.update(extra_query)
        # 加时间戳绕过可能的客户端缓存 / 服务端中间层缓存
        query.setdefault("timestamp", str(int(__import__("time").time() * 1000)))

        resp = self.http.post(url, params=query)

        if resp.status_code in (401, 403):
            raise TokenExpired(f"登录态失效（HTTP {resp.status_code}）")
        if resp.status_code == 429:
            raise RateLimited("选课系统限流")

        try:
            data = resp.json()
        except ValueError as exc:
            if _looks_like_login_page(resp.text or ""):
                # 选课系统在未登录 / Token 失效时，不会返回 401，
                # 而是**返回 200 + 应用首页的 HTML**。这是最容易踩的坑：
                # 只看状态码会把「登录失效」误判成「接口返回了奇怪的东西」。
                raise TokenExpired("选课系统返回了页面而非数据，登录态已失效") from exc

            if allow_form_fallback:
                logger.debug("接口 %s 对 query 参数无响应，改用 form body 重试", path)
                return self._call_via_form(path, setting_key=setting_key, payload=payload)

            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise ApiError(f"接口 {path} 返回了非 JSON 内容：{snippet}", payload=snippet) from exc

        if not isinstance(data, dict):
            raise ApiError(f"接口 {path} 返回结构异常：{type(data).__name__}")

        self._raise_for_code(path, data)
        return data

    def _call_via_form(self, path: str, *, setting_key: str, payload: str) -> Any:
        """降级路径：把参数放进 form body 再试一次。

        注意这里不再递归回 ``_call``，避免两种模式互相回退造成无限循环。
        """
        url = self._url(path)
        resp = self.http.post(
            url,
            data={setting_key: payload, "timestamp": str(int(__import__("time").time() * 1000))},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code in (401, 403):
            raise TokenExpired(f"登录态失效（HTTP {resp.status_code}）")
        if resp.status_code == 429:
            raise RateLimited("选课系统限流")

        try:
            data = resp.json()
        except ValueError as exc:
            if _looks_like_login_page(resp.text or ""):
                raise TokenExpired("选课系统返回了页面而非数据，登录态已失效") from exc
            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise ApiError(
                f"接口 {path} 在 query 与 form 两种方式下都返回了非 JSON 内容：{snippet}",
                payload=snippet,
            ) from exc

        if not isinstance(data, dict):
            raise ApiError(f"接口 {path} 返回结构异常：{type(data).__name__}")

        self._raise_for_code(path, data)
        return data

    @staticmethod
    def _raise_for_code(path: str, data: dict) -> None:
        """根据响应 code 抛出语义化异常。"""
        code = data.get("code")
        msg = str(data.get("msg") or data.get("message") or "")
        if code is None:
            return
        code_str = str(code)
        if code_str in _CODE_SUCCESS:
            return

        lowered = msg.lower()
        if any(token in msg for token in ("登录", "未登录", "超时", "认证")) or "token" in lowered:
            raise TokenExpired(f"接口 {path} 要求重新登录：{msg}")
        if "频繁" in msg or "限制" in msg:
            raise RateLimited(f"接口 {path} 被限流：{msg}")
        if code_str in _CODE_BATCH_ERROR and ("批次" in msg or "轮次" in msg or "阶段" in msg):
            raise NotInBatchError(msg or f"接口 {path} 批次不可用")

        raise ApiError(f"接口 {path} 返回错误：code={code} msg={msg}", code=code, payload=data)

    # ================================================================ 学生 / 批次

    def student_info(self) -> dict:
        """取学生信息（含 ``electiveBatchList`` 批次列表）。"""
        paths = ("student/xkxf.do", "elective/studentstatus.do")
        last_error: Exception | None = None
        for path in paths:
            try:
                data = self._call(path, setting_key="querySetting", setting={"data": {}})
            except (ApiError, NotInBatchError) as exc:
                last_error = exc
                continue
            info = data.get("data")
            if isinstance(info, dict) and info:
                return info
        if last_error:
            raise last_error
        raise ApiError("无法获取学生信息：所有候选接口都没有返回 data")

    def batches(self) -> list[Batch]:
        """取全部选课批次。"""
        info = self.student_info()
        raw = _pick(info, "electiveBatchList", "batchList", "batches") or []
        return [Batch.from_api(item) for item in raw if isinstance(item, dict)]

    def current_batch(self) -> Batch:
        """取当前可选批次；没有任何可选批次时抛 :class:`NotInBatchError`。"""
        all_batches = self.batches()
        for batch in all_batches:
            if batch.can_select:
                return batch
        detail = "；".join(str(b) for b in all_batches) or "（接口未返回任何批次）"
        raise NotInBatchError(f"当前不在可选课时间内。已知批次：{detail}")

    # ================================================================ 课程查询

    def _query_path(self, teaching_class_type: str) -> str:
        if teaching_class_type in CourseType.PROGRAM_TYPES:
            return "elective/programCourse.do"
        return "elective/publicCourse.do"

    def query_courses(
        self,
        keyword: str,
        *,
        teaching_class_type: str = CourseType.PUBLIC,
        batch_code: str,
        student_code: str,
        page_size: int = 50,
        page_number: int = 0,
        check_conflict: str = "0",
        check_capacity: str = "0",
    ) -> list[Course]:
        """按关键词查询课程。

        Args:
            keyword: 课程名关键词（支持模糊匹配，服务端行为）。
            teaching_class_type: 见 :class:`CourseType`。
            batch_code: 批次号，来自 :meth:`current_batch`。
            student_code: 学号。
            check_conflict: ``'0'`` 不过滤冲突，``'1'`` 过滤掉冲突课程。
            check_capacity: ``'0'`` 返回全部（含已满），``'1'`` 只返回有余量的。
                轮询**必须**用 ``'0'``，否则满课时列表为空、无法观察余量变化。
        """
        setting = {
            "data": {
                "studentCode": student_code,
                "campus": self.campus,
                "electiveBatchCode": batch_code,
                "isMajor": "1",
                "teachingClassType": teaching_class_type,
                "checkConflict": check_conflict,
                "checkCapacity": check_capacity,
                "queryContent": keyword,
            },
            "pageSize": str(page_size),
            "pageNumber": str(page_number),
            "order": "",
        }
        data = self._call(
            self._query_path(teaching_class_type),
            setting_key="querySetting",
            setting=setting,
        )

        items = _pick(data, "dataList", "list", "rows", "data") or []
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
        teaching_class_type: str = CourseType.PUBLIC,
        batch_code: str,
        student_code: str,
        exact_match: bool = True,
    ) -> list[TeachingClass]:
        """按课程名找到所有教学班。

        ``exact_match=True`` 时只保留课程名与关键词完全一致的课
        （避免「大学语文」误匹配「大学语文（进阶）」）。
        """
        courses = self.query_courses(
            keyword,
            teaching_class_type=teaching_class_type,
            batch_code=batch_code,
            student_code=student_code,
            check_capacity="0",  # 必须带上已满的课，否则看不到余量
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
        teaching_class_type: str = CourseType.PUBLIC,
        course_name: str = "",
        operation_type: str = "1",
    ) -> SelectionResult:
        """提交选课。

        Returns:
            :class:`SelectionResult` —— 注意：**业务失败不抛异常**，
            而是用 ``outcome`` 表达，因为「已满」是轮询中的正常状态。
        """
        setting = {
            "data": {
                "operationType": operation_type,
                "studentCode": student_code,
                "electiveBatchCode": batch_code,
                "teachingClassId": teaching_class_id,
                "isMajor": "1",
                "campus": self.campus,
                "teachingClassType": teaching_class_type,
            }
        }

        try:
            data = self._call(
                "elective/volunteer.do",
                setting_key="addParam",
                setting=setting,
            )
        except TokenExpired:
            raise
        except RateLimited:
            raise
        except ApiError as exc:
            # 接口用非 0 code 表达的业务失败，在这里翻译成 outcome
            return SelectionResult(
                outcome=_classify_error(str(exc), exc.code),
                message=str(exc),
                code=exc.code,
                teaching_class_id=teaching_class_id,
                course_name=course_name,
                raw=exc.payload if isinstance(exc.payload, dict) else {},
            )

        return self._parse_submit_response(data, teaching_class_id, course_name)

    @staticmethod
    def _parse_submit_response(
        data: dict, teaching_class_id: str, course_name: str
    ) -> SelectionResult:
        """把选课接口响应翻译成 :class:`SelectionResult`。"""
        code = data.get("code")
        msg = str(data.get("msg") or data.get("message") or "")
        code_str = str(code) if code is not None else ""

        if code_str in _CODE_SUCCESS and ("成功" in msg or not msg):
            outcome = SelectionOutcome.SUCCESS
        elif code_str in _CODE_SUCCESS:
            outcome = _classify_error(msg, code)
            if outcome is SelectionOutcome.ERROR and "成功" in msg:
                outcome = SelectionOutcome.SUCCESS
        else:
            outcome = _classify_error(msg, code)

        return SelectionResult(
            outcome=outcome,
            message=msg or outcome.label,
            code=code,
            teaching_class_id=teaching_class_id,
            course_name=course_name,
            raw=data,
        )

    # ================================================================ 已选课程

    def selected_courses(self, *, batch_code: str, student_code: str) -> list[dict]:
        """取本批次已选课程（用于确认是否真的选上了）。"""
        candidates = (
            ("elective/course.do", "querySetting"),
            ("elective/queryCourse.do", "querySetting"),
        )
        setting = {
            "data": {
                "studentCode": student_code,
                "campus": self.campus,
                "electiveBatchCode": batch_code,
                "isMajor": "1",
                "teachingClassType": "",
                "queryContent": "",
            },
            "pageSize": "100",
            "pageNumber": "0",
            "order": "",
        }
        for path, key in candidates:
            try:
                data = self._call(path, setting_key=key, setting=setting)
            except (ApiError, NotInBatchError):
                continue
            items = _pick(data, "dataList", "list", "rows") or []
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

    if any(word in text for word in ("频繁", "限制", "稍后")):
        return SelectionOutcome.RATE_LIMITED

    if any(word in text for word in ("登录", "认证", "超时", "token", "Token")):
        return SelectionOutcome.AUTH_ERROR

    if str(code) in _CODE_BATCH_ERROR:
        return SelectionOutcome.NOT_IN_BATCH
    return SelectionOutcome.ERROR


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
