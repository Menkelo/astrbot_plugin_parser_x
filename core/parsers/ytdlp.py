from __future__ import annotations

from abc import ABC
from pathlib import Path
from re import Match
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
        value = self.config.get("cookies", {}).get("ytdlp_cookie_file", [])
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if isinstance(item, dict):
                item = item.get("path") or item.get("file") or item.get("name")
            if not isinstance(item, str) or not item.strip():
                continue
            path = Path(item).expanduser()
            if path.is_file():
                return path
        return None

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

        author_name = info.author_name or info.uploader or info.channel or "未知作者"
        author = self.create_author(author_name)
        description = (info.description or "").strip()
        if len(description) > 240:
            description = description[:237] + "..."

        max_size_mb = int(self.config.get("performance", {}).get("source_max_size", 90))
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
        extra = {
            "adapter": "yt-dlp",
            "render_text_card": True,
            "text_card_media": info.thumbnail or "",
            "card_kind": "音频" if self.media_kind == "audio" else "视频",
            "card_author_badge": "作者",
            "card_info": [
                "音频文件独立发送" if self.media_kind == "audio" else "视频文件独立发送"
            ],
        }
        if self.media_kind == "video":
            extra["video_separate_from_card"] = True
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

    @handle("acfun.cn", r"https?://(?:www\.|m\.)?acfun\.cn/v/ac\d+[^\s<>]*")
    async def parse_acfun(self, searched: Match[str]):
        return await self._parse_ytdlp(searched)


class NeteaseMusicParser(YtDlpParser):
    platform = Platform(name="netease_music", display_name="网易云音乐")
    media_kind = "audio"

    @handle("music.163.com", r"https?://(?:y\.)?music\.163\.com/[^\s<>]+")
    @handle("163cn.tv", r"https?://163cn\.tv/[^\s<>]+")
    async def parse_netease(self, searched: Match[str]):
        return await self._parse_ytdlp(searched)
