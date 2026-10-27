import json
import re
import time
from typing import Any, ClassVar, NoReturn
from urllib.parse import unquote, urlencode, urljoin, urlparse

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from msgspec import Struct, convert, field

from ..card_flow import apply_author_block, build_card_extra
from ..comment_settings import CommentSettings
from ..data import DynamicContent
from ..html_renderer import HtmlRenderService
from ..topic_text import strip_topic_tags
from ..video_codec import codec_preference, prefer_candidates


from ..download import Downloader
from .base import BaseParser, ParseException, Platform, SkipParseException, handle
from .gallery import first_media_url, live_photo, motion_url, ordered_media_plan
from .xiaohongshu_comment import XiaohongshuCommentFeed


class XiaoHongShuParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="xiaohongshu", display_name="小红书")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

        # 评论区（默认关闭；需在「平台登录态」填写小红书 Cookie 才会启用）。
        cookies = config.get("cookies", {})
        self.comment_cookie = (
            str(cookies.get("xiaohongshu_cookie", ""))
            if isinstance(cookies, dict)
            else ""
        )
        # legacy_enabled=False：配置里没有这个开关时默认关闭。
        comment_settings = CommentSettings.from_config(
            config, "xiaohongshu", legacy_enabled=False
        )
        self.enable_comment_card = comment_settings.enabled
        self.comment_limit = comment_settings.display_count
        self.comment_timeout = comment_settings.timeout
        self.render_service = HtmlRenderService.from_config(config)

        self.comment_feed = XiaohongshuCommentFeed(
            self,
            limit=self.comment_limit,
        )

        self._page_cache_ttl = int(config.get("xhs_cache_ttl", 120))

        self._page_cache: dict[str, tuple[float, str, str]] = {}
        self._redirect_cache: dict[str, tuple[float, str]] = {}

        explore_headers = {
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            ),
            "referer": "https://www.xiaohongshu.com/",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        }
        self.headers.update(explore_headers)

        discovery_headers = {
            "origin": "https://www.xiaohongshu.com",
            "referer": "https://www.xiaohongshu.com/",
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        self.ios_headers.update(discovery_headers)

        # 小红书会校验请求头与 TLS 指纹的一致性：伪装成其它浏览器但握手指纹
        # 对不上的请求会被判为爬虫，返回空壳页（表现为“分享链接失效”）。
        # curl_cffi 的指纹自带匹配的 UA，这里统一不再覆盖它。
        for _headers in (self.headers, self.ios_headers, self.android_headers):
            _headers.pop("User-Agent", None)

    @staticmethod
    def _note_owner_id(user: object) -> str:
        """从 note 的 user 字段取作者 ID（两种数据形态的字段名都兼容）。"""
        if not isinstance(user, (dict,)):
            return ""
        for key in ("userId", "user_id", "id", "uid"):
            value = user.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    def _comment_extra(

        self,
        note_id: str,
        xsec_token: str,
        *,
        title: str,
        cover: str | None,
        owner_id: str | int | None,
    ) -> dict:
        """评论区任务（图文与视频笔记都挂，且已开启并配置 Cookie 时才挂载）。"""
        if (
            not self.enable_comment_card
            or not self.comment_cookie
            or not note_id
            or not xsec_token
        ):
            return {}

        async def build_comment_document():
            return await self.comment_feed.build_document(
                str(note_id),
                xsec_token,
                work_title=title,
                cover=cover,
                owner_id=owner_id,
            )

        return {
            "text_card_comment_factory": build_comment_document,
            "comment_timeout": self.comment_timeout,
        }

    def set_render_service(self, render_service: HtmlRenderService) -> None:
        self.render_service = render_service

    # region cache


    def _cache_get_page(self, url: str) -> tuple[str, str] | None:
        item = self._page_cache.get(url)
        if not item:
            return None

        ts, final_url, html = item
        if time.time() - ts > self._page_cache_ttl:
            self._page_cache.pop(url, None)
            return None

        return final_url, html

    def _cache_set_page(self, url: str, final_url: str, html: str):
        self._page_cache[url] = (time.time(), final_url, html)
        # 按插入顺序淘汰最旧项，避免一次性 clear 造成缓存抖动（命中率骤降）
        while len(self._page_cache) > 128:
            self._page_cache.pop(next(iter(self._page_cache)), None)

    def _cache_get_redirect(self, url: str) -> str | None:
        item = self._redirect_cache.get(url)
        if not item:
            return None

        ts, final_url = item
        if time.time() - ts > self._page_cache_ttl:
            self._redirect_cache.pop(url, None)
            return None

        return final_url

    def _cache_set_redirect(self, url: str, final_url: str):
        self._redirect_cache[url] = (time.time(), final_url)
        while len(self._redirect_cache) > 256:
            self._redirect_cache.pop(next(iter(self._redirect_cache)), None)

    # endregion

    @staticmethod
    def _uniq_urls(urls: list[str]) -> list[str]:
        seen = set()
        out = []

        for u in urls:
            if not isinstance(u, str) or not u:
                continue

            u = u.replace("&amp;", "&")
            if u not in seen:
                seen.add(u)
                out.append(u)

        return out

    @staticmethod
    def _normalize_timestamp(value: object) -> int | None:
        try:
            timestamp = int(value or 0)
        except (TypeError, ValueError):
            return None
        if timestamp <= 0:
            return None
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return timestamp

    @staticmethod
    def _normalize_note_text(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        # 小红书序列化话题为 #名称[话题]#；只移除协议标记，保留可高亮话题。
        text = re.sub(r"#([^#\r\n]+?)\[话题\]#", r"#\1", text)
        return strip_topic_tags(text)

    @staticmethod
    def _is_live_url(url: str | None) -> bool:
        u = (url or "").lower()
        return "xiaohongshu.com/livestream" in u or "/livestream/" in u

    def _source_mentions_live(self) -> bool:
        text = self.source_text or ""
        low = text.lower()
        return any(
            key in text
            for key in (
                "正在直播",
                "直接观看直播",
                "观看直播",
                "来看直播",
            )
        ) or any(
            key in low
            for key in (
                "xiaohongshu.com/livestream",
                "/livestream/",
            )
        )

    _XSEC_TOKEN_RE = re.compile(r"xsec_token=([^&\s\"'<>]+)", re.I)
    _NOTE_SHARE_RE = re.compile(
        r"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/"
        r"(?P<id>[0-9a-zA-Z]+)(?:\?(?P<query>[A-Za-z0-9._%&+=/#@-]*))?",
        re.I,
    )

    @staticmethod
    def _unwrap_sec_redirect(url: str) -> str:
        """
        小红书安全中转页：
            https://www.xiaohongshu.com/404/sec_xxx?source=xhs_sec_server&originalUrl=<URL编码真实地址>
            https://www.xiaohongshu.com/404?source=/404/sec_xxx?redirectPath=<URL编码真实地址>
        真实笔记地址（含 xsec_token）被编码塞进 originalUrl / redirectPath。取出并解码一次返回；
        非该形态则原样返回。用 unquote（而非 unquote_plus），避免把 token 里的 '+' 误转成空格。
        """
        for marker in ("originalUrl=", "redirectPath="):
            idx = url.find(marker)
            if idx == -1:
                continue
            raw = url[idx + len(marker) :]
            amp = raw.find("&")
            if amp != -1:
                raw = raw[:amp]
            decoded = unquote(raw)
            if decoded.startswith("http://www.xiaohongshu.com"):
                decoded = "https://" + decoded[len("http://") :]
            if decoded:
                return decoded
        return url

    @staticmethod
    def _iter_query_pairs(query: str):
        for part in (query or "").split("&"):
            if not part:
                continue
            if "=" in part:
                name, value = part.split("=", 1)
            else:
                name, value = part, ""
            yield unquote(name), unquote(value)

    @staticmethod
    def _query_value(url: str, key: str) -> str:
        if not url or key not in url:
            return ""
        parsed = urlparse(url)
        for name, value in XiaoHongShuParser._iter_query_pairs(parsed.query):
            if name == key and value:
                return value
        unwrapped = XiaoHongShuParser._unwrap_sec_redirect(url)
        if unwrapped != url:
            return XiaoHongShuParser._query_value(unwrapped, key)
        return ""

    @staticmethod
    def _with_query(url: str, **extra: str) -> str:
        parsed = urlparse(url)
        pairs = list(XiaoHongShuParser._iter_query_pairs(parsed.query))
        existing = {name for name, _ in pairs}
        for name, value in extra.items():
            if value and name not in existing:
                pairs.append((name, value))
        return parsed._replace(query=urlencode(pairs)).geturl()

    @staticmethod
    def _unwrap_vue_ref(value: Any) -> Any:
        seen = 0
        while isinstance(value, dict) and seen < 6:
            inner = value.get("value")
            if inner is None:
                inner = value.get("_value")
            if inner is None:
                break
            if len(value) > 3 and not (
                isinstance(inner, dict)
                and (
                    "noteDetailMap" in inner
                    or "noteData" in inner
                    or "type" in inner
                    or "imageList" in inner
                )
            ):
                break
            value = inner
            seen += 1
        return value

    def _collect_xsec_token(self, *urls: str, xhs_id: str = "") -> str:
        # Only tokens attached to this note's URL may be reused. A source message
        # can contain several links (or a bare token belonging to another note).
        shares = []
        for text in (*urls, self.source_text or ""):
            text = self._unwrap_sec_redirect(text or "").replace("&amp;", "&")
            shares.extend(self._NOTE_SHARE_RE.finditer(text))
        if not xhs_id and shares:
            xhs_id = shares[0].group("id")
        for matched in shares:
            if matched.group("id") == xhs_id:
                token = self._query_value(matched.group(0), "xsec_token")
                if token:
                    return token
        return ""

    def _ensure_note_url(self, url: str, xhs_id: str = "") -> str:
        url = self._unwrap_sec_redirect(url)
        if re.search(r"xiaohongshu\.com/404(?:/|\?|$)", url, re.I):
            return url
        matched_url = self._NOTE_SHARE_RE.fullmatch(url)
        if not matched_url or (xhs_id and matched_url.group("id") != xhs_id):
            return url
        xhs_id = xhs_id or matched_url.group("id")
        token = self._query_value(url, "xsec_token")
        source = ""
        if not token and xhs_id:
            for matched in self._NOTE_SHARE_RE.finditer(self.source_text or ""):
                if matched.group("id") != xhs_id:
                    continue
                source = matched.group(0)
                token = self._collect_xsec_token(source)
                if token:
                    break
        extra: dict[str, str] = {}
        if token:
            extra["xsec_token"] = token
            extra["xsec_source"] = (
                self._query_value(url, "xsec_source")
                or self._query_value(source, "xsec_source")
                or "pc_share"
            )
        if extra:
            url = self._with_query(url, **extra)
        return url

    @staticmethod
    def _looks_like_note(item: dict) -> bool:
        if item.get("imageList") or item.get("imagesList") or item.get("video"):
            return True
        if item.get("type") in {"normal", "video"}:
            return True
        if item.get("title") and isinstance(item.get("user"), dict):
            return True
        if item.get("noteId") or item.get("id"):
            return bool(item.get("desc") or item.get("title") or item.get("user"))
        return False

    def _as_note_dict(self, item: Any) -> dict:
        item = self._unwrap_vue_ref(item)
        if not isinstance(item, dict) or not item:
            return {}
        inner = self._unwrap_vue_ref(item.get("note"))
        if isinstance(inner, dict) and self._looks_like_note(inner):
            return inner
        if self._looks_like_note(item):
            return item
        return {}

    @staticmethod
    def _is_discovery_note(note: dict) -> bool:
        user = note.get("user")
        return isinstance(user, dict) and "nickName" in user and "nickname" not in user

    def _extract_note_from_state(
        self, json_obj: dict, xhs_id: str | None = None
    ) -> dict:
        json_obj = self._unwrap_vue_ref(json_obj)
        if not isinstance(json_obj, dict):
            return {}

        candidates: list[tuple[Any, str]] = []
        note_container = self._unwrap_vue_ref(json_obj.get("note") or {})
        if not isinstance(note_container, dict):
            note_container = {}
        detail_map = self._unwrap_vue_ref(note_container.get("noteDetailMap") or {})
        if not isinstance(detail_map, dict):
            detail_map = {}
        for key, item in detail_map.items():
            candidates.append((item, str(key)))
        for key in ("firstNote", "note", "currentNote"):
            candidates.append((note_container.get(key), ""))

        note_data_root = self._unwrap_vue_ref(json_obj.get("noteData") or {})
        if isinstance(note_data_root, dict):
            data = self._unwrap_vue_ref(note_data_root.get("data") or {})
            if isinstance(data, dict):
                candidates.append((data.get("noteData"), ""))
                candidates.append((data.get("note"), ""))
                candidates.append((data, ""))
            candidates.append((note_data_root.get("noteData"), ""))
            candidates.append((note_data_root.get("normalNotePreloadData"), ""))
            error_note = note_data_root.get("errorNoteData")
            if isinstance(error_note, dict):
                candidates.append((error_note.get("noteData"), ""))

        anonymous: list[dict] = []
        has_identified_note = False
        claimed = False
        for item, map_id in candidates:
            note = self._as_note_dict(item)
            if not note:
                continue
            # Only the note's own identifiers prove identity. A noteDetailMap
            # key may be a placeholder such as "other", so it counts as an
            # opaque key instead of a competing id: demanding an exact set
            # equality dropped real notes whose key was not the note id.
            own_ids = {str(note[key]) for key in ("noteId", "id") if note.get(key)}
            wrapper = self._unwrap_vue_ref(item)
            if isinstance(wrapper, dict):
                own_ids.update(str(wrapper[key]) for key in ("noteId", "id")
                               if wrapper.get(key))
            ids = set(own_ids)
            if map_id:
                ids.add(map_id)
            if ids:
                has_identified_note = True
                if not xhs_id or not own_ids or xhs_id in own_ids:
                    if own_ids and xhs_id:
                        claimed = True
                    return note
            elif note not in anonymous:
                anonymous.append(note)
        if not claimed and not has_identified_note and len(anonymous) == 1:
            return anonymous[0]
        return {}

    @staticmethod
    def _blocked_page_reason(
        url: str,
        html: str,
        json_obj: dict | None = None,
    ) -> str | None:
        final = unquote(url or "")
        html_text = html or ""
        if (
            "error_code=300031" in final
            or "暂时无法浏览" in final
            or "暂时无法浏览" in html_text
        ):
            return "当前笔记暂时无法浏览（可能已删除、设为私密或分享链接已失效）"
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
        title = ""
        if title_match:
            title = re.sub(r"\s+", " ", title_match.group(1)).strip()
        if "页面不见了" in title:
            return "小红书分享链接失效或内容已删除"
        if re.search(r"xiaohongshu\.com/404(?:/|\?|$)", final, re.I):
            return "小红书分享链接失效或内容已删除"
        # A rejected session lands on /website-login/error with a plain
        # 「小红书」title, so the checks above miss it and the parser used to
        # report a deleted note for what is really a login requirement.
        if re.search(r"xiaohongshu\.com/website-login/(?:error|captcha)", final, re.I):
            return "小红书要求登录后才能浏览（分享链接需要有效 Cookie）"
        if not isinstance(json_obj, dict):
            return None
        if json_obj.get("notFoundPage"):
            return "小红书分享链接失效或内容已删除"
        note = json_obj.get("note") if isinstance(json_obj.get("note"), dict) else {}
        info = (
            note.get("serverRequestInfo")
            if isinstance(note.get("serverRequestInfo"), dict)
            else {}
        )
        err = str(info.get("errMsg") or "").strip()
        code = info.get("errorCode")
        if code not in (0, "0", None, "") and err:
            return err
        note_data = json_obj.get("noteData")
        if isinstance(note_data, dict) and note_data.get("hasError"):
            return "小红书分享链接失效或内容已删除"
        return None

    @staticmethod
    def _debug_note_locations(json_obj: dict) -> str:
        """调试用：定向汇总 note 可能所在的几个位置（仅键名/类型，不打印大段内容）。"""
        try:
            json_obj = XiaoHongShuParser._unwrap_vue_ref(json_obj)
            if not isinstance(json_obj, dict):
                return f"top={type(json_obj).__name__}"
            note_raw = XiaoHongShuParser._unwrap_vue_ref(json_obj.get("note"))
            note_obj = note_raw if isinstance(note_raw, dict) else {}
            detail_raw = XiaoHongShuParser._unwrap_vue_ref(
                note_obj.get("noteDetailMap")
            )
            detail_map = detail_raw if isinstance(detail_raw, dict) else {}
            sample: dict = {}
            if detail_map:
                first = next(iter(detail_map.values()))
                if isinstance(first, dict):
                    sample["entry_keys"] = list(first.keys())
                    inner = first.get("note")
                    if isinstance(inner, dict):
                        sample["note_keys"] = list(inner.keys())
                        sample["type"] = inner.get("type")
            note_data_raw = XiaoHongShuParser._unwrap_vue_ref(json_obj.get("noteData"))
            note_data_obj = note_data_raw if isinstance(note_data_raw, dict) else {}
            return (
                f"top={list(json_obj.keys())}, "
                f"note_keys={list(note_obj.keys())}, "
                f"detailMap_ids={list(detail_map.keys())}, "
                f"noteData_keys={list(note_data_obj.keys())}, "
                f"sample={sample}"
            )
        except Exception as e:
            return f"<shape error: {e}>"

    @staticmethod
    def _trusted_document_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            return (
                parsed.scheme == "https"
                and parsed.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}
                and parsed.port in (None, 443)
                and not parsed.username and not parsed.password
            )
        except ValueError:
            return False

    async def _request_html(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: int = 10,
        with_cookie: bool = False,
    ) -> tuple[str, str]:
        # Never let a transport forward an explicit Cookie across redirects.
        # Cookie stays request-local; CDN/media headers remain unauthenticated.
        for _ in range(6):
            if not self._trusted_document_url(url):
                raise ParseException("小红书页面跳转到不可信地址")
            request_headers = {
                key: value for key, value in headers.items() if key.lower() != "cookie"
            }
            # Note pages are fetched anonymously by default: a stale logged-in
            # Cookie makes xiaohongshu.com redirect to /website-login/error
            # ("分享链接失效"), while the same page parses fine without it.
            # with_cookie=True is the fallback for login-only notes.
            if with_cookie and self.comment_cookie:
                request_headers["Cookie"] = self.comment_cookie
            resp = await self.http_get(
                url, headers=request_headers, allow_redirects=False, timeout=timeout,
            )
            final_url = str(resp.url)
            if not self._trusted_document_url(final_url):
                raise ParseException("小红书页面跳转到不可信地址")
            if resp.status_code in (301, 302, 303, 307, 308):
                location = next((value for key, value in resp.headers.items()
                                 if key.lower() == "location"), "")
                if not location:
                    raise ParseException("小红书页面跳转缺少地址")
                url = urljoin(final_url, location)
                continue
            if resp.status_code >= 400:
                raise ParseException(f"小红书页面请求失败: HTTP {resp.status_code}")
            html = resp.text or ""
            if not html:
                raise ParseException("小红书页面为空")
            return final_url, html
        raise ParseException("小红书页面跳转次数过多")

    async def _fetch_html(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: int = 10,
        xhs_id: str = "",
    ) -> tuple[str, str]:
        url = self._ensure_note_url(url, xhs_id)
        cached = self._cache_get_page(url)
        if cached:
            return cached

        final_url, html = await self._request_html(
            url, headers=headers, timeout=timeout
        )
        retry_url = self._ensure_note_url(
            self._unwrap_sec_redirect(final_url),
            xhs_id,
        )
        if retry_url != url:
            try:
                retry_final, retry_html = await self._request_html(
                    retry_url, headers=headers, timeout=timeout
                )
            except ParseException:
                retry_final, retry_html = "", ""
            if retry_html and not self._blocked_page_reason(retry_final, retry_html):
                final_url, html = retry_final, retry_html

        # Login-only notes never reach the anonymous page: retry once with the
        # configured Cookie and only accept it when it actually beats the
        # anonymous result. The anonymous page is never replaced by a worse one.
        if self.comment_cookie and self._blocked_page_reason(final_url, html):
            try:
                cookie_final, cookie_html = await self._request_html(
                    url, headers=headers, timeout=timeout, with_cookie=True
                )
            except ParseException:
                cookie_final, cookie_html = "", ""
            if (cookie_html
                    and not self._blocked_page_reason(cookie_final, cookie_html)
                    and self._extract_initial_state_json(cookie_html)):
                final_url, html = cookie_final, cookie_html

        self._cache_set_page(url, final_url, html)
        return final_url, html

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]*")
    @handle("xhslink.cn", r"xhslink\.cn/[A-Za-z0-9._?%&+=/#@-]*")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"

        if self._source_mentions_live():
            raise SkipParseException()

        final_url = self._cache_get_redirect(url)
        if not final_url:
            final_url = await self.get_final_url(url, headers=self.ios_headers)
            final_url = self._ensure_note_url(final_url)
            self._cache_set_redirect(url, final_url)

        if self._is_live_url(final_url):
            raise SkipParseException()

        if final_url == url:
            raise ParseException(f"小红书短链跳转失败: {url}")

        keyword, matched = self.search_url(final_url)
        return await self.parse(keyword, matched)

    @handle(
        "hongshu.com/explore",
        r"explore/(?P<xhs_id>[0-9a-zA-Z]+)(?:\?[A-Za-z0-9._%&+=/#@-]*)?",
    )
    async def _parse_explore(self, searched: re.Match[str]):
        route = searched.group(0)
        xhs_id = searched.group("xhs_id")
        url = self._ensure_note_url(
            f"https://www.xiaohongshu.com/{route}",
            xhs_id,
        )
        try:
            return await self.parse_explore(url, xhs_id)
        except ParseException as e:
            if self._is_terminal_xhs_error(e):
                raise
            logger.debug(f"parse_explore failed, fallback to discovery: {e}")
            discovery_url = url.replace("/explore/", "/discovery/item/", 1)
            return await self.parse_discovery(discovery_url, xhs_id)

    @handle(
        "hongshu.com/discovery/item/",
        r"discovery/item/(?P<xhs_id>[0-9a-zA-Z]+)(?:\?[A-Za-z0-9._%&+=/#@-]*)?",
    )
    async def _parse_discovery(self, searched: re.Match[str]):
        route = searched.group(0)
        xhs_id = searched.group("xhs_id")

        # discovery 优先转 explore，通常更稳更快
        explore_route = route.replace("discovery/item", "explore", 1)
        explore_url = self._ensure_note_url(
            f"https://www.xiaohongshu.com/{explore_route}",
            xhs_id,
        )

        try:
            return await self.parse_explore(explore_url, xhs_id)
        except ParseException as e:
            if self._is_terminal_xhs_error(e):
                raise
            logger.debug(f"parse_explore failed, fallback to discovery: {e}")
            return await self.parse_discovery(
                self._ensure_note_url(
                    f"https://www.xiaohongshu.com/{route}",
                    xhs_id,
                ),
                xhs_id,
            )

    @staticmethod
    def _is_terminal_xhs_error(exc: ParseException) -> bool:
        msg = str(exc)
        return any(
            key in msg
            for key in (
                "暂时无法浏览",
                "分享链接失效",
                "内容已删除",
                "要求登录",
            )
        )

    def _process_note(self, note_data: dict, final_url: str | None = None):
        if self._is_discovery_note(note_data):
            return self._process_discovery_data(note_data, {}, final_url)
        return self._process_explore_data(note_data, final_url)

    def _raise_missing_note(
        self,
        *,
        kind: str,
        xhs_id: str | None,
        final_url: str,
        html: str,
        json_obj: dict,
    ) -> NoReturn:
        blocked = self._blocked_page_reason(final_url, html, json_obj)
        if blocked:
            raise ParseException(blocked)
        logger.warning(
            f"[XHS] {kind} 未找到 note (xhs_id={xhs_id}): "
            f"{self._debug_note_locations(json_obj)}"
        )
        raise ParseException("小红书笔记详情为空，请换一条带完整分享链接的笔记再试")

    async def parse_explore(self, url: str, xhs_id: str):
        final_url, html = await self._fetch_html(
            url,
            headers=self.headers,
            timeout=10,
            xhs_id=xhs_id,
        )
        logger.debug(f"[XHS] explore url: {final_url}")

        json_obj = self._extract_initial_state_json(html, final_url)
        note_data = self._extract_note_from_state(json_obj, xhs_id)
        if not note_data:
            self._raise_missing_note(
                kind="explore",
                xhs_id=xhs_id,
                final_url=final_url,
                html=html,
                json_obj=json_obj,
            )
        return self._process_note(note_data, final_url)

    async def parse_discovery(self, url: str, xhs_id: str | None = None):
        final_url, html = await self._fetch_html(
            url,
            headers=self.ios_headers,
            timeout=10,
            xhs_id=xhs_id or "",
        )
        logger.debug(f"[XHS] discovery url: {final_url}")

        json_obj = self._extract_initial_state_json(html, final_url)
        note_data = self._extract_note_from_state(json_obj, xhs_id)
        if note_data:
            if self._is_discovery_note(note_data):
                preload_data = json_obj.get("noteData", {}).get(
                    "normalNotePreloadData", {}
                )
                if not isinstance(preload_data, dict):
                    preload_data = {}
                return self._process_discovery_data(note_data, preload_data, final_url)
            return self._process_explore_data(note_data, final_url)

        self._raise_missing_note(
            kind="discovery",
            xhs_id=xhs_id,
            final_url=final_url,
            html=html,
            json_obj=json_obj,
        )

    def _gallery_contents(self, note_data: dict):
        contents = []
        seen = set()
        for item in note_data.get("imageList") or note_data.get("imagesList") or []:
            if not isinstance(item, dict):
                continue
            still = next(
                (
                    url
                    for key in ("urlDefault", "urlSizeLarge", "url", "urlPre")
                    if (url := first_media_url(item.get(key)))
                ),
                None,
            )
            if not still:
                still = first_media_url(item.get("infoList"))
            motion = motion_url(
                {"stream": item.get("stream")}, codec_preference(self.config)
            )
            key = ("live" if motion else "image", motion or still)
            if key in seen or not key[1]:
                continue
            seen.add(key)
            if motion:
                contents.append(live_photo(self, motion, still, headers=self.headers))
            elif still:
                contents.extend(self.create_image_contents([still]))
        return contents

    @staticmethod
    def _gallery_extra(contents, flow=None, *, cover=None, is_video=False) -> dict:
        # 无论图文还是视频都出正文卡：媒体本身直发/进投递计划，
        # 卡片作为「头图 + 正文」与评论区一起进合并转发。
        # flow 由调用方按「正文 + 图…」给出：单图升为头图，多图走堆叠拆图。
        extra: dict = build_card_extra(
            flow or [],
            cover=cover,
            is_video=is_video,
        )
        if any(isinstance(item, DynamicContent) for item in contents):
            extra["native_delivery"] = True
        return extra

    @classmethod
    def _gallery_delivery(
        cls,
        contents,
        comment_extra: dict | None = None,
        *,
        flow=None,
        cover=None,
        is_video=False,
        avatar: str | None = None,
        timestamp: object = None,
        location: str | None = None,
    ):
        """图文/动图的原生投递计划，并把评论区任务合并进同一个 extra。"""
        merged = dict(
            cls._gallery_extra(
                contents, flow, cover=cover, is_video=is_video
            )
        )
        # 卡片署名行：头像 + 发布日期 + 归属地
        merged.update(
            apply_author_block(
                merged, avatar=avatar, timestamp=timestamp, location=location
            )
        )
        if comment_extra:
            merged.update(comment_extra)
        if not isinstance(contents, list) or not any(
            isinstance(item, DynamicContent) for item in contents
        ):
            return {"extra": merged} if merged else {}
        return {
            "delivery": ordered_media_plan(contents),
            "extra": merged,
        }

    def _process_explore_data(
        self,
        note_data: dict,
        final_url: str | None = None,
    ):
        class Image(Struct):
            urlDefault: str | None = None
            urlPre: str | None = None
            url: str | None = None

        class User(Struct):
            nickname: str
            avatar: str | None = None

        class NoteDetail(Struct):
            # msgspec 限制：必填字段必须在有默认值字段之前
            type: str
            user: User

            title: str = ""
            desc: str = ""
            imageList: list[Image] = field(default_factory=list)
            video: Video | None = None
            time: int | None = None

            @property
            def nickname(self) -> str:
                return self.user.nickname

            @property
            def avatar_url(self) -> str | None:
                return self.user.avatar

            @property
            def image_urls(self) -> list[str]:
                urls = []
                for item in self.imageList:
                    u = item.urlDefault or item.urlPre or item.url
                    if u:
                        urls.append(u)
                return XiaoHongShuParser._uniq_urls(urls)

            @property
            def video_url(self) -> str | None:
                if self.type != "video" or not self.video:
                    return None
                return self.video.video_url

        note_detail = convert(note_data, type=NoteDetail)
        note_title = self._normalize_note_text(note_detail.title)
        note_text = self._normalize_note_text(note_detail.desc)

        contents = []
        note_image_urls = note_detail.image_urls

        if video_url := (
            note_detail.video.select_video_url(codec_preference(self.config))
            if note_detail.type == "video" and note_detail.video else None
        ):
            cover_url = note_image_urls[0] if note_image_urls else None
            contents.append(self.create_video_content(video_url, cover_url))

        else:
            contents.extend(self._gallery_contents(note_data))

        author = self.create_author(note_detail.nickname, note_detail.avatar_url)

        # 图文和视频笔记都挂评论区：卡片会把评论区拼到正文下方，
        # 不再只给视频笔记出评论。
        comment_extra = self._comment_extra(
            str(note_data.get("noteId") or note_data.get("id") or ""),
            self._collect_xsec_token(
                final_url or "",
                xhs_id=str(note_data.get("noteId") or note_data.get("id") or ""),
            ),
            title=note_title,
            cover=note_image_urls[0] if note_image_urls else None,
            owner_id=self._note_owner_id(note_detail.user),
        )


        return self.result(
            title=note_title,
            text=note_text,
            author=author,
            contents=contents,
            timestamp=self._normalize_timestamp(note_detail.time),
            url=final_url,
            **self._gallery_delivery(
                contents,
                comment_extra,
                flow=[
                    ("text", note_text or ""),
                    *[("image", url) for url in note_image_urls],
                ],
                cover=(note_image_urls[0] if note_image_urls else None),
                is_video=bool(note_detail.video_url),
                avatar=note_detail.avatar_url,
                timestamp=self._normalize_timestamp(note_detail.time),
            ),
        )


    def _process_discovery_data(
        self,
        note_data: dict,
        preload_data: dict,
        final_url: str | None = None,
    ):
        class Image(Struct):
            url: str | None = None
            urlSizeLarge: str | None = None
            urlDefault: str | None = None

        class User(Struct):
            nickName: str
            avatar: str | None = None

        class NoteData(Struct):
            # msgspec 限制：必填字段必须在有默认值字段之前
            type: str
            user: User

            title: str = ""
            desc: str = ""
            time: int = 0
            lastUpdateTime: int = 0
            imageList: list[Image] = field(default_factory=list)
            video: Video | None = None

            @property
            def image_urls(self) -> list[str]:
                urls = []
                for item in self.imageList:
                    u = item.urlSizeLarge or item.urlDefault or item.url
                    if u:
                        urls.append(u)
                return XiaoHongShuParser._uniq_urls(urls)

            @property
            def video_url(self) -> str | None:
                if self.type != "video" or not self.video:
                    return None
                return self.video.video_url

        class NormalNotePreloadData(Struct):
            title: str = ""
            desc: str = ""
            imagesList: list[Image] = field(default_factory=list)

            @property
            def image_urls(self) -> list[str]:
                urls = []
                for item in self.imagesList:
                    u = item.urlSizeLarge or item.urlDefault or item.url
                    if u:
                        urls.append(u)
                return XiaoHongShuParser._uniq_urls(urls)

        note_data_obj = convert(note_data, type=NoteData)
        note_title = self._normalize_note_text(note_data_obj.title)
        note_text = self._normalize_note_text(note_data_obj.desc)

        contents = []
        image_urls = list(note_data_obj.image_urls or [])

        if video_url := (
            note_data_obj.video.select_video_url(codec_preference(self.config))
            if note_data_obj.type == "video" and note_data_obj.video else None
        ):
            if preload_data:
                preload_obj = convert(preload_data, type=NormalNotePreloadData)
                img_urls = preload_obj.image_urls
            else:
                img_urls = note_data_obj.image_urls

            contents.append(
                self.create_video_content(
                    video_url,
                    img_urls[0] if img_urls else None,
                )
            )

        else:
            contents.extend(self._gallery_contents(note_data))

        return self.result(
            title=note_title,
            author=self.create_author(
                note_data_obj.user.nickName, note_data_obj.user.avatar
            ),
            contents=contents,
            text=note_text,
            timestamp=self._normalize_timestamp(note_data_obj.time),
            url=final_url,
            **self._gallery_delivery(
                contents,
                self._discovery_comment_extra(
                    note_data, note_data_obj, note_title, contents, final_url
                ),
                # 正文 + 全部图片：单图升头图、多图按堆叠拆图，
                # 与详情页那条路径保持同一套编排。
                flow=[
                    ("text", note_text or ""),
                    *[("image", url) for url in image_urls],
                ],
                cover=(
                    note_data_obj.image_urls[0]
                    if note_data_obj.image_urls
                    else None
                ),
                is_video=bool(note_data_obj.video_url),
                avatar=note_data_obj.user.avatar,
                timestamp=self._normalize_timestamp(note_data_obj.time),
            ),
        )


    def _discovery_comment_extra(
        self,
        note_data: dict,
        note_data_obj: object,
        title: str,
        contents: list,
        final_url: str | None,
    ) -> dict:
        """discovery 形态同样图文与视频都挂评论区。"""
        cover = None
        for content in contents:
            cover = getattr(content, "cover_url", None) or getattr(
                content, "cover", None
            )
            if cover:
                break
        return self._comment_extra(
            str(note_data.get("noteId") or note_data.get("id") or ""),
            self._collect_xsec_token(
                final_url or "",
                xhs_id=str(note_data.get("noteId") or note_data.get("id") or ""),
            ),
            title=title,
            cover=cover,
            owner_id=self._note_owner_id(getattr(note_data_obj, "user", None)),
        )

    def _extract_initial_state_json(
        self, html: str, page_url: str = ""
    ) -> dict[str, Any]:

        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        matched = re.search(pattern, html, re.DOTALL)

        if not matched:
            # A login/captcha shim has no state blob either. The marker only
            # survives in the final URL, so report why instead of blaming the
            # share link.
            if re.search(r"website-login/(?:error|captcha)", page_url or "", re.I):
                raise ParseException(
                    "小红书要求登录后才能浏览（分享链接需要有效 Cookie）"
                )
            raise ParseException("小红书分享链接失效或内容已删除")

        # Complete JSON strings win over bare tokens, including escaped quotes.
        # JSON still validates the result: no JavaScript evaluation is performed.
        json_str = re.sub(
            r'"(?:[^"\\]|\\.)*"|(?<![\w$])undefined(?![\w$])',
            lambda token: "null" if token.group(0) == "undefined" else token.group(0),
            matched.group(1),
        )
        try:
            payload = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ParseException("小红书分享链接失效或内容已删除") from exc
        payload = self._unwrap_vue_ref(payload)
        if not isinstance(payload, dict):
            raise ParseException("小红书分享链接失效或内容已删除")
        return payload


class Stream(Struct):
    h264: list[dict[str, Any]] | None = None
    h265: list[dict[str, Any]] | None = None
    av1: list[dict[str, Any]] | None = None
    h266: list[dict[str, Any]] | None = None


class Media(Struct):
    stream: Stream


class Video(Struct):
    media: Media

    @staticmethod
    def _score_video_url(url: str) -> tuple[int, int]:
        """
        分数越小越优先。
        优先低清/轻量 URL，避免默认拿 masterUrl / m3u8 拖慢。
        """
        u = url.lower()

        quality_score = 50

        if any(k in u for k in ("360", "ld", "low", "lowest")):
            quality_score = 10
        elif any(k in u for k in ("480", "sd", "standard")):
            quality_score = 20
        elif any(k in u for k in ("540",)):
            quality_score = 25
        elif any(k in u for k in ("720", "hd")):
            quality_score = 40
        elif any(k in u for k in ("1080", "fhd", "uhd", "2k", "4k")):
            quality_score = 90

        # masterUrl / m3u8 作为最后选择
        type_score = 0
        if "master" in u or ".m3u8" in u:
            type_score = 50

        return quality_score, type_score

    @classmethod
    def _collect_urls_from_stream_item(cls, item: dict[str, Any]) -> list[str]:
        urls: list[str] = []

        # 优先收集轻量/直链类字段
        for key in (
            "url",
            "playUrl",
            "play_url",
            "streamUrl",
            "stream_url",
            "mainUrl",
            "main_url",
        ):
            val = item.get(key)
            if isinstance(val, str) and val:
                urls.append(val)

        # backup 也可能是直接可播地址
        for key in ("backupUrls", "backup_urls", "backupUrl", "backup_url"):
            val = item.get(key)
            if isinstance(val, list):
                for u in val:
                    if isinstance(u, str) and u:
                        urls.append(u)
            elif isinstance(val, str) and val:
                urls.append(val)

        # 最后才放 masterUrl
        val = item.get("masterUrl")
        if isinstance(val, str) and val:
            urls.append(val)

        seen = set()
        uniq = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)

        uniq.sort(key=cls._score_video_url)
        return uniq

    @property
    def video_url(self) -> str | None:
        return self.select_video_url()

    def select_video_url(self, preference: str = "auto") -> str | None:
        stream = self.media.stream

        # h264 优先，兼容性最好；之后再考虑 h265/av1/h266
        groups = [
            stream.h264 or [],
            stream.h265 or [],
            stream.av1 or [],
            stream.h266 or [],
        ]

        candidates: list[tuple[str, str]] = []
        for codec, group in zip(("avc", "hevc", "av1", "h266"), groups):
            for item in group:
                if not isinstance(item, dict):
                    continue
                candidates.extend(
                    (url, codec) for url in self._collect_urls_from_stream_item(item)
                )
        candidates = prefer_candidates(candidates, preference, lambda item: item[1])
        all_urls = [url for url, _ in candidates]

        if not all_urls:
            return None

        all_urls = list(dict.fromkeys(all_urls))
        all_urls.sort(key=self._score_video_url)
        return all_urls[0]
