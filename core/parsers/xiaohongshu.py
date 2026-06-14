import json
import re
import time
from typing import Any, ClassVar
from urllib.parse import unquote

from msgspec import Struct, convert, field

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..download import Downloader
from .base import BaseParser, ParseException, Platform, handle


class XiaoHongShuParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="xiaohongshu", display_name="小红书")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

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
    def _unwrap_sec_redirect(url: str) -> str:
        """
        小红书安全中转页：
            https://www.xiaohongshu.com/404/sec_xxx?source=xhs_sec_server&originalUrl=<URL编码真实地址>
        真实笔记地址（含 xsec_token）被编码塞进 originalUrl 参数里。这里取出并解码一次返回；
        非该形态则原样返回。用 unquote（而非 parse_qs/unquote_plus），避免把 token 里的 '+' 误转成空格。
        """
        marker = "originalUrl="
        idx = url.find(marker)
        if idx == -1:
            return url

        raw = url[idx + len(marker):]
        # originalUrl 内部的 & 都是 %26，遇到字面 & 即为外层下一个参数，截断即可
        amp = raw.find("&")
        if amp != -1:
            raw = raw[:amp]

        decoded = unquote(raw)
        return decoded or url

    @staticmethod
    def _debug_note_locations(json_obj: dict) -> str:
        """调试用：定向汇总 note 可能所在的几个位置（仅键名/类型，不打印大段内容）。"""
        try:
            note_obj = json_obj.get("note") if isinstance(json_obj.get("note"), dict) else {}
            detail_map = (
                note_obj.get("noteDetailMap")
                if isinstance(note_obj.get("noteDetailMap"), dict)
                else {}
            )
            sample: dict = {}
            if detail_map:
                first = next(iter(detail_map.values()))
                if isinstance(first, dict):
                    sample["entry_keys"] = list(first.keys())
                    inner = first.get("note")
                    if isinstance(inner, dict):
                        sample["note_keys"] = list(inner.keys())
                        sample["type"] = inner.get("type")
            note_data_obj = (
                json_obj.get("noteData") if isinstance(json_obj.get("noteData"), dict) else {}
            )
            return (
                f"top={list(json_obj.keys())}, "
                f"note_keys={list(note_obj.keys())}, "
                f"detailMap_ids={list(detail_map.keys())}, "
                f"noteData_keys={list(note_data_obj.keys())}, "
                f"sample={sample}"
            )
        except Exception as e:
            return f"<shape error: {e}>"

    async def _fetch_html(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: int = 10,
    ) -> tuple[str, str]:
        cached = self._cache_get_page(url)
        if cached:
            return cached

        resp = await self.http_get(
            url,
            headers=headers,
            allow_redirects=True,
            timeout=timeout,
        )

        html = resp.text or ""
        final_url = str(resp.url)

        if resp.status_code >= 400:
            raise ParseException(f"小红书页面请求失败: HTTP {resp.status_code}")

        if not html:
            raise ParseException("小红书页面为空")

        self._cache_set_page(url, final_url, html)
        return final_url, html

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]*")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"

        final_url = self._cache_get_redirect(url)
        if not final_url:
            final_url = await self.get_final_url(url, headers=self.ios_headers)
            # 解包 /404/sec 安全中转页，取出 originalUrl 真实笔记地址（保留 xsec_token）
            final_url = self._unwrap_sec_redirect(final_url)
            self._cache_set_redirect(url, final_url)

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
        url = f"https://www.xiaohongshu.com/{route}"
        xhs_id = searched.group("xhs_id")
        return await self.parse_explore(url, xhs_id)

    @handle(
        "hongshu.com/discovery/item/",
        r"discovery/item/(?P<xhs_id>[0-9a-zA-Z]+)(?:\?[A-Za-z0-9._%&+=/#@-]*)?",
    )
    async def _parse_discovery(self, searched: re.Match[str]):
        route = searched.group(0)
        xhs_id = searched.group("xhs_id")

        # discovery 优先转 explore，通常更稳更快
        explore_route = route.replace("discovery/item", "explore", 1)
        explore_url = f"https://www.xiaohongshu.com/{explore_route}"

        try:
            return await self.parse_explore(explore_url, xhs_id)
        except ParseException as e:
            logger.debug(f"parse_explore failed, fallback to discovery: {e}")
            return await self.parse_discovery(
                f"https://www.xiaohongshu.com/{route}",
                xhs_id,
            )

    async def parse_explore(self, url: str, xhs_id: str):
        final_url, html = await self._fetch_html(
            url,
            headers=self.headers,
            timeout=10,
        )
        logger.debug(f"[XHS] explore url: {final_url}")

        json_obj = self._extract_initial_state_json(html)

        note_data = (
            json_obj.get("note", {})
            .get("noteDetailMap", {})
            .get(xhs_id, {})
            .get("note", {})
        )

        if not note_data:
            # 有时候 key 不是 xhs_id，直接取第一个 note
            detail_map = json_obj.get("note", {}).get("noteDetailMap", {})
            if isinstance(detail_map, dict) and detail_map:
                first_key = next(iter(detail_map))
                note_data = detail_map.get(first_key, {}).get("note", {})

        if not note_data:
            logger.warning(
                f"[XHS] explore 未找到 note (xhs_id={xhs_id}): {self._debug_note_locations(json_obj)}"
            )
            raise ParseException("can't find note detail in json_obj")

        return self._process_explore_data(note_data, final_url)

    async def parse_discovery(self, url: str, xhs_id: str | None = None):
        final_url, html = await self._fetch_html(
            url,
            headers=self.ios_headers,
            timeout=10,
        )
        logger.debug(f"[XHS] discovery url: {final_url}")

        json_obj = self._extract_initial_state_json(html)

        note_data = json_obj.get("noteData", {}).get("data", {}).get("noteData", {})
        if note_data:
            preload_data = json_obj.get("noteData", {}).get("normalNotePreloadData", {})
            return self._process_discovery_data(note_data, preload_data, final_url)

        note_container = json_obj.get("note", {})
        detail_map = note_container.get("noteDetailMap", {})

        if xhs_id:
            note_data = detail_map.get(xhs_id, {}).get("note", {})
            if note_data:
                return self._process_explore_data(note_data, final_url)

        if detail_map:
            first_key = next(iter(detail_map))
            note_data = detail_map[first_key].get("note", {})
            if note_data:
                return self._process_explore_data(note_data, final_url)

        note_data = note_container.get("firstNote", {}) or note_container.get("note", {})
        if note_data:
            return self._process_explore_data(note_data, final_url)

        logger.warning(
            f"[XHS] discovery 未找到 note (xhs_id={xhs_id}): {self._debug_note_locations(json_obj)}"
        )
        raise ParseException("解析异常: can't find noteData in noteData.data or noteDetailMap")

    def _process_explore_data(self, note_data: dict, final_url: str | None = None):
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

        contents = []

        if video_url := note_detail.video_url:
            cover_url = note_detail.image_urls[0] if note_detail.image_urls else None
            contents.append(self.create_video_content(video_url, cover_url))

        elif image_urls := note_detail.image_urls:
            contents.extend(self.create_image_contents(image_urls))

        author = self.create_author(note_detail.nickname, note_detail.avatar_url)

        return self.result(
            title=note_detail.title,
            text=note_detail.desc,
            author=author,
            contents=contents,
            timestamp=note_detail.time // 1000 if note_detail.time else None,
            url=final_url,
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

        contents = []

        if video_url := note_data_obj.video_url:
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

        elif img_urls := note_data_obj.image_urls:
            contents.extend(self.create_image_contents(img_urls))

        return self.result(
            title=note_data_obj.title,
            author=self.create_author(note_data_obj.user.nickName, note_data_obj.user.avatar),
            contents=contents,
            text=note_data_obj.desc,
            timestamp=note_data_obj.time // 1000 if note_data_obj.time else None,
            url=final_url,
        )

    def _extract_initial_state_json(self, html: str) -> dict[str, Any]:
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        matched = re.search(pattern, html, re.DOTALL)

        if not matched:
            raise ParseException("小红书分享链接失效或内容已删除")

        json_str = matched.group(1).replace("undefined", "null")
        return json.loads(json_str)


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
        stream = self.media.stream

        # h264 优先，兼容性最好；之后再考虑 h265/av1/h266
        groups = [
            stream.h264 or [],
            stream.h265 or [],
            stream.av1 or [],
            stream.h266 or [],
        ]

        all_urls: list[str] = []

        for group in groups:
            for item in group:
                if not isinstance(item, dict):
                    continue
                all_urls.extend(self._collect_urls_from_stream_item(item))

        if not all_urls:
            return None

        all_urls = list(dict.fromkeys(all_urls))
        all_urls.sort(key=self._score_video_url)
        return all_urls[0]
