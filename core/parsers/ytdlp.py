from __future__ import annotations

from abc import ABC
from json import JSONDecoder
from pathlib import Path
from re import Match, search
from typing import ClassVar, Literal

from astrbot.api import logger

from ..data import AudioContent, Platform, VideoContent
from ..exception import ParseException, SkipParseException
from .base import BaseParser, handle


class YtDlpParser(BaseParser, ABC):
    """rconsole-plugin 长尾站点的 yt-dlp 兼容层。

    核心站点继续使用专用解析器；此层只承接 yt-dlp 已有成熟 extractor
    的平台，使上游新增站点时通常只需增加一个小型路由类。
    """

    media_kind: ClassVar[Literal["video", "audio"]] = "video"

    def _cookie_file(self) -> Path | None:
        # Authentication is platform-specific; no global uploaded cookie file.
        return None

    def _ytdlp_author_name(self, info) -> str:
        return info.author_name or info.uploader or info.channel or "未知作者"

    async def _parse_ytdlp(self, searched: Match[str]):
        url = searched.group(0).rstrip(").,;!?，。；！？）]")
        low_url = url.lower()
        if any(
            token in low_url for token in ("/live/", "live.douyin", "live.kuaishou")
        ):
            raise SkipParseException()

        cookiefile = self._cookie_file()
        try:
            info = await self.downloader.ytdlp_extract_info(
                url,
                cookiefile,
                force_generic_extractor=False,
            )
        except Exception as exc:
            raise ParseException(
                f"{self.platform.display_name} 解析失败: {exc}"
            ) from exc

        author_name = self._ytdlp_author_name(info)
        author = self.create_author(author_name)
        description = (info.description or "").strip()
        if len(description) > 240:
            description = description[:237] + "..."

        from ..config_policy import SOURCE_MAX_SIZE_MB
        max_size_mb = SOURCE_MAX_SIZE_MB
        if self.media_kind == "audio":
            task = self.downloader.download_audio(
                url,
                use_ytdlp=True,
                cookiefile=cookiefile,
                max_size_mb=max_size_mb,
            )
            contents = [AudioContent(task, duration=info.duration or 0)]
        else:
            task = self.downloader.download_video(
                url,
                use_ytdlp=True,
                cookiefile=cookiefile,
                max_size_mb=max_size_mb,
            )
            contents = [VideoContent(task, duration=info.duration or 0)]

        logger.info(f"Parser X 使用 yt-dlp 适配 {self.platform.display_name}: {url}")
        # 视频本体直发，正文卡带上封面与简介进合并转发。
        extra = {"adapter": "yt-dlp", "render_text_card": True}
        return self.result(
            title=info.title,
            text=description or None,
            author=author,
            timestamp=info.timestamp,
            url=url,
            contents=contents,
            extra=extra,
        )


class AcFunParser(YtDlpParser):
    platform = Platform(name="acfun", display_name="AcFun")

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        from ..comment_settings import CommentSettings
        from .acfun_comment import AcFunCommentFeed

        raw_cookies = config.get("cookies", {})
        cookie = raw_cookies.get("acfun_cookie", "") if isinstance(raw_cookies, dict) else ""
        self.acfun_cookie = (
            cookie.replace("\r", "").replace("\n", "").strip()
            if isinstance(cookie, str) else ""
        )
        self._acfun_cookiefile: Path | None = None
        self.comment_settings = CommentSettings.from_config(config, "acfun")
        self.comment_feed = AcFunCommentFeed(self)

    def acfun_headers(self) -> dict[str, str]:
        # Keep credentials off generic headers used for third-party images/CDNs.
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() != "cookie"}
        if self.acfun_cookie:
            headers["Cookie"] = self.acfun_cookie
        return headers

    def _cookie_file(self) -> Path | None:
        if not self.acfun_cookie:
            return None
        if self._acfun_cookiefile is None or not self._acfun_cookiefile.is_file():
            from tempfile import NamedTemporaryFile

            from ..utils import cookies_str_to_netscape

            # Unique per parser; tempfile creates it owner-only (0600).
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="acfun_cookies_",
                suffix=".txt", dir=self.cache_dir, delete=False,
            ) as stream:
                stream.write(cookies_str_to_netscape(self.acfun_cookie, ".acfun.cn"))
                self._acfun_cookiefile = Path(stream.name)
        return self._acfun_cookiefile

    def _ytdlp_author_name(self, info) -> str:
        # Use the raw uploader, never strip a potentially genuine "Unknown@" nickname.
        return info.uploader or info.channel or "未知作者"

    @handle("acfun.cn", r"https?://(?:www\.|m\.)?acfun\.cn/v/ac\d+[^\s<>]*")
    async def parse_acfun(self, searched: Match[str]):
        result = await self._parse_ytdlp(searched)
        # Public page metadata is more accurate than yt-dlp's synthetic channel name.
        # Decode only the assigned JSON value: the script contains other objects too.
        try:
            response = await self.http_get(
                result.url, headers=self.acfun_headers(), timeout=8, retries=1,
            )
            if response.status_code == 200:
                assignment = search(r"window\.videoInfo\s*=\s*", response.text)
                if assignment:
                    metadata, _ = JSONDecoder().raw_decode(
                        response.text[assignment.end():]
                    )
                    user = metadata.get("user")
                    if not isinstance(user, dict):
                        user = {}
                    name = user.get("name")
                    avatar = user.get("headUrl")
                    cover = metadata.get("coverUrl")
                    if isinstance(name, str) and name.strip():
                        result.author.name = name.strip()
                    if isinstance(avatar, str) and avatar.strip():
                        result.extra["text_card_avatar"] = avatar.strip()
                    if isinstance(cover, str) and cover.strip():
                        result.extra["text_card_media"] = cover.strip()
                        result.extra["text_card_media_fit"] = "cover"
        except Exception as exc:
            logger.debug(f"AcFun 公开元数据补全失败，保留 yt-dlp 结果: {exc}")
        if self.comment_settings.enabled:
            import re

            matched = re.search(r"/v/ac(\d+)", searched.group(0))
            if matched:
                source_id = matched.group(1)

                async def build_comment_document():
                    return await self.comment_feed.fetch_document(source_id)

                result.extra.update({
                    "text_card_comment_factory": build_comment_document,
                    "comment_timeout": self.comment_settings.timeout,
                })
        return result

