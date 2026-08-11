from __future__ import annotations

import re
from html import unescape
from re import Match
from typing import Any, ClassVar

from ..data import ImageContent, Platform, VideoContent
from ..exception import ParseException
from ..utils import normalize_image_url
from .base import BaseParser, handle
from .metadata import parse_open_graph


class TiebaParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="tieba", display_name="贴吧")

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        self.api_base = str(
            config.get("integrations", {}).get("tieba_api_base", "") or ""
        ).rstrip("/")
        self.headers.update(
            {
                "Referer": "https://tieba.baidu.com/",
                "Accept": "text/html,application/xhtml+xml,application/json",
            }
        )

    @staticmethod
    def _clean_text(text: str | None) -> str:
        if not text:
            return ""
        text = re.sub(r"<br\s*/?>", "\n", str(text), flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"[ \t]+", " ", unescape(text)).strip()

    @staticmethod
    def _official_images(html: str) -> list[str]:
        urls = re.findall(
            r"(?:data-original|data-src|src)=[\"'](https?://[^\"']+)[\"']",
            html or "",
            flags=re.IGNORECASE,
        )
        out = []
        for url in urls:
            low = url.lower()
            if not any(
                host in low
                for host in (
                    "tiebapic.baidu.com/forum/",
                    "imgsa.baidu.com/forum/",
                    "hiphotos.baidu.com/",
                )
            ):
                continue
            normalized = normalize_image_url(unescape(url))
            if normalized and normalized not in out:
                out.append(normalized)
            if len(out) >= 12:
                break
        return out

    @staticmethod
    def _parse_api_post(payload: dict[str, Any]) -> dict[str, Any]:
        post_list = payload.get("post_list") or (payload.get("data") or {}).get(
            "post_list"
        )
        if not isinstance(post_list, list) or not post_list:
            raise ParseException("贴吧详情接口没有返回帖子内容")
        top = post_list[0]
        texts: list[str] = []
        images: list[str] = []
        videos: list[str] = []
        for item in top.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("text"):
                texts.append(TiebaParser._clean_text(item["text"]))
            if normalized := normalize_image_url(item.get("cdn_src")):
                images.append(normalized)
            if item.get("link") and str(item["link"]).startswith("http"):
                videos.append(item["link"])
        return {
            "title": top.get("title") or "贴吧帖子",
            "text": "\n".join(item for item in texts if item),
            "images": list(dict.fromkeys(images)),
            "videos": list(dict.fromkeys(videos)),
            "author": top.get("author_name") or top.get("user_name"),
        }

    @handle("tieba.baidu.com", r"https?://tieba\.baidu\.com/p/(\d+)[^\s<>]*")
    async def parse_tieba(self, searched: Match[str]):
        url = searched.group(0).rstrip(").,;!?，。；！？）]")
        thread_id = searched.group(1)
        parsed: dict[str, Any] | None = None
        if self.api_base:
            try:
                response = await self.http_get(
                    f"{self.api_base}/tieba/post_detail",
                    params={"tid": thread_id},
                    headers=self.headers,
                    timeout=15,
                    retries=2,
                )
                if response.status_code < 400:
                    parsed = self._parse_api_post(response.json())
            except Exception:
                parsed = None

        if parsed is None:
            response = await self.http_get(
                url,
                headers=self.headers,
                timeout=15,
                retries=2,
            )
            if response.status_code >= 400:
                raise ParseException(f"贴吧页面请求失败: HTTP {response.status_code}")
            metadata = parse_open_graph(response.text)
            title = re.sub(
                r"[_-](?:百度)?贴吧.*$", "", metadata["title"] or "贴吧帖子"
            ).strip()
            images = self._official_images(response.text)
            if metadata["image"]:
                images.insert(0, metadata["image"])
            parsed = {
                "title": title,
                "text": self._clean_text(metadata["description"]),
                "images": list(dict.fromkeys(images)),
                "videos": [],
                "author": None,
            }

        contents = [
            ImageContent(self.downloader.download_img(item, ext_headers=self.headers))
            for item in parsed["images"][:12]
        ]
        for index, video_url in enumerate(parsed["videos"][:2], start=1):
            contents.append(
                VideoContent(
                    self.downloader.download_video(
                        video_url,
                        video_name=f"tieba_{thread_id}_{index}.mp4",
                        ext_headers=self.headers,
                        max_size_mb=int(
                            self.config.get("performance", {}).get(
                                "source_max_size", 90
                            )
                        ),
                    )
                )
            )
        return self.result(
            title=parsed["title"],
            author=self.create_author(parsed["author"]) if parsed["author"] else None,
            text=parsed["text"] or None,
            contents=contents,
            url=url,
            extra={
                "send_text": True,
                "info": (
                    "已使用配置的详情接口提取楼主内容。"
                    if self.api_base
                    else "未配置贴吧详情接口，当前使用官方页面元数据解析。"
                ),
            },
        )


__all__ = ["TiebaParser"]
