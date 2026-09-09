"""bilibili_api 常见返回码 -> 用户可读提示。

bilibili_api 的 ResponseCodeException.__str__ 会附加整个原始响应 dict
（例如稿件失效时 view 接口返回 code=62012、message="62012"），直接把异常
对象塞进 ParseException 透传给用户会暴露难以理解的报文。各解析器统一从
这里取文案，subject 用于区分"稿件/动态/内容"等上下文名词。
"""

from bilibili_api.exceptions import ResponseCodeException

from ...exception import ParseException

_BILI_API_CODE_HINTS: dict[int, str] = {
    -404: "该{subject}不存在或已被删除",
    62012: "该{subject}不存在或已被删除",
    62002: "该{subject}当前不可见（可能审核中、被锁定或仅发布者可见）",
    -101: "未登录无法访问该{subject}，请配置有效的 bili_ck Cookie",
    -403: "访问该{subject}权限不足，可能需登录或会员权限，请配置 bili_ck 后重试",
    -352: "请求触发 B站风控，建议配置 bili_ck 后重试",
    -412: "请求被 B站风控拦截，请稍后重试",
    -799: "请求过于频繁，请稍后再试",
}


def bili_api_parse_exc(exc: Exception, subject: str = "内容") -> ParseException:
    """把 bilibili_api 抛出的异常转成不携带原始报文的 ParseException。

    对已收录错误码返回对应友好提示；其余情况只保留单行 msg（数字型 message
    如 "62012" 无含义，直接丢弃）或错误码，不再带整个响应 dict。
    """
    if isinstance(exc, ResponseCodeException):
        hint = _BILI_API_CODE_HINTS.get(exc.code)
        if hint:
            return ParseException(hint.format(subject=subject))

        msg = exc.msg
        if isinstance(msg, str) and msg.strip() and not msg.strip().isdigit():
            return ParseException(f"B站 API 请求失败: {msg.strip()}")
        return ParseException(f"B站 API 请求失败（错误码 {exc.code}）")

    return ParseException(f"B站 API 请求失败: {exc}")
