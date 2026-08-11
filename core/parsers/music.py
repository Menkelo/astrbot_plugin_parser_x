from __future__ import annotations

import json
import re
from abc import ABC
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from re import Match
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from ..data import AudioContent, ImageContent, Platform
from ..exception import ParseException
from ..utils import normalize_image_url
from .base import BaseParser, handle


class _OpenGraphParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attrs_dict = {key.lower(): value for key, value in attrs if value}
        if tag.lower() == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            content = attrs_dict.get("content")
            if key and content and key not in self.meta:
                self.meta[key] = unescape(content).strip()
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str):
        if self._in_title and data.strip():
            self.title_parts.append(data.strip())

    @property
    def title(self) -> str | None:
        value = self.meta.get("og:title") or " ".join(self.title_parts).strip()
        return value or None

    @property
    def description(self) -> str | None:
        value = self.meta.get("og:description") or self.meta.get("description")
        return value or None

    @property
    def image(self) -> str | None:
        return normalize_image_url(
            self.meta.get("og:image") or self.meta.get("twitter:image")
        )


def parse_open_graph(html: str) -> dict[str, str | None]:
    parser = _OpenGraphParser()
    parser.feed(html or "")
    return {
        "title": parser.title,
        "description": parser.description,
        "image": parser.image,
    }


def _duration_text(seconds: int | float | None) -> str | None:
    if not seconds:
        return None
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _first_string(value: Any, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in keys and isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            if found := _first_string(item, keys):
                return found
    elif isinstance(value, list):
        for item in value:
            if found := _first_string(item, keys):
                return found
    return None


def _first_audio_url(value: Any) -> str | None:
    if isinstance(value, str):
        low = value.lower().split("?", 1)[0]
        if value.startswith(("http://", "https://")) and low.endswith(
            (".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav")
        ):
            return value
        return None
    if isinstance(value, dict):
        preferred = {
            "play_url",
            "playurl",
            "audio_url",
            "audiourl",
            "backup_url",
            "backupurl",
            "url",
        }
        for key, item in value.items():
            if key.lower() in preferred and (found := _first_audio_url(item)):
                return found
        for item in value.values():
            if found := _first_audio_url(item):
                return found
    elif isinstance(value, list):
        for item in value:
            if found := _first_audio_url(item):
                return found
    return None


class MusicParser(BaseParser, ABC):
    headers: dict[str, str]

    async def _json_get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self.http_get(
            url,
            params=params,
            headers=headers or self.headers,
            timeout=15,
            retries=2,
        )
        if response.status_code >= 400:
            raise ParseException(f"音乐平台请求失败: HTTP {response.status_code}")
        try:
            data = response.json()
        except Exception as exc:
            raise ParseException("音乐平台返回了无效数据") from exc
        if not isinstance(data, dict):
            raise ParseException("音乐平台返回格式异常")
        return data

    def _cover_contents(self, url: str | None) -> list[ImageContent]:
        url = normalize_image_url(url)
        if not url:
            return []
        return [
            ImageContent(self.downloader.download_img(url, ext_headers=self.headers))
        ]

    @staticmethod
    def _metadata_extra(info: str) -> dict[str, Any]:
        return {"send_text": True, "info": info}


class QQMusicParser(MusicParser):
    platform: ClassVar[Platform] = Platform(name="qq_music", display_name="QQ音乐")

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "Referer": "https://y.qq.com/",
                "Origin": "https://y.qq.com",
            }
        )
        cookie = config.get("cookies", {}).get("qq_music_cookie", "")
        if cookie:
            self.headers["Cookie"] = cookie

    @staticmethod
    def extract_song_identity(url: str) -> tuple[str | None, str | None]:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        song_mid = next(
            (query[key][0] for key in ("songmid", "song_mid", "mid") if query.get(key)),
            None,
        )
        song_id = next(
            (query[key][0] for key in ("songid", "song_id") if query.get(key)),
            None,
        )
        if not song_mid:
            matched = re.search(r"/songDetail/([A-Za-z0-9]+)", parsed.path)
            if matched:
                song_mid = matched.group(1)
        return song_mid, song_id

    @handle(
        "y.qq.com",
        r"https?://(?:[ci]\.)?y\.qq\.com/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    )
    @handle(
        "c.y.qq.com",
        r"https?://c\.y\.qq\.com/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    )
    @handle(
        "i.y.qq.com",
        r"https?://i\.y\.qq\.com/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    )
    async def parse_qq_music(self, searched: Match[str]):
        original_url = searched.group(0).rstrip(").,;!?，。；！？）]")
        final_url = await self.get_final_url(original_url, self.headers, retries=2)
        song_mid, song_id = self.extract_song_identity(final_url)
        if not song_mid and not song_id:
            metadata = await self._page_metadata(final_url)
            if metadata["title"]:
                return self.result(
                    title=metadata["title"],
                    text=metadata["description"],
                    contents=self._cover_contents(metadata["image"]),
                    url=final_url,
                    extra=self._metadata_extra(
                        "未从分享链接中识别到歌曲编号，仅展示页面元数据。"
                    ),
                )
            raise ParseException("QQ音乐分享链接中没有歌曲编号")

        params = {"format": "json"}
        if song_mid:
            params["songmid"] = song_mid
        else:
            params["songid"] = song_id
        data = await self._json_get(
            "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg",
            params=params,
        )
        songs = data.get("data") or []
        if data.get("code") != 0 or not isinstance(songs, list) or not songs:
            raise ParseException("QQ音乐未返回歌曲详情")

        song = songs[0]
        album = song.get("album") or {}
        singers = song.get("singer") or []
        singer_name = " / ".join(
            item.get("name", "") for item in singers if isinstance(item, dict)
        ).strip(" /")
        album_mid = album.get("mid")
        cover = (
            f"https://y.qq.com/music/photo_new/T002R800x800M000{album_mid}.jpg"
            if album_mid
            else None
        )
        details = [
            f"专辑：{album.get('name')}" if album.get("name") else None,
            f"时长：{_duration_text(song.get('interval'))}"
            if _duration_text(song.get("interval"))
            else None,
            f"发行：{song.get('time_public')}" if song.get("time_public") else None,
        ]
        return self.result(
            title=song.get("title") or song.get("name") or "QQ音乐歌曲",
            author=self.create_author(singer_name or "QQ音乐"),
            text="\n".join(item for item in details if item),
            contents=self._cover_contents(cover),
            url=final_url,
            extra=self._metadata_extra(
                "已使用 QQ 音乐官方接口获取元数据；完整音源受账号权限和版权限制。"
            ),
        )

    async def _page_metadata(self, url: str) -> dict[str, str | None]:
        response = await self.http_get(url, headers=self.headers, timeout=15, retries=1)
        if response.status_code >= 400:
            return {"title": None, "description": None, "image": None}
        return parse_open_graph(response.text)


class KugouMusicParser(MusicParser):
    platform: ClassVar[Platform] = Platform(name="kugou_music", display_name="酷狗音乐")

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        self.cookie = config.get("cookies", {}).get("kugou_cookie", "")
        integrations = config.get("integrations", {})
        self.api_server = str(integrations.get("kugou_api_server", "")).rstrip("/")
        self.quality = integrations.get("kugou_audio_quality", "128") or "128"

    @staticmethod
    def parse_share_data(html: str) -> dict[str, str]:
        result: dict[str, str] = {}
        matched = re.search(
            r"var\s+dataFromSmarty\s*=\s*(\[[\s\S]*?\])\s*,?\s*(?://|;)",
            html or "",
        )
        if matched:
            try:
                items = json.loads(matched.group(1))
                item = items[0] if items else {}
                if isinstance(item, dict):
                    result = {
                        "author": str(item.get("author_name") or "").strip(),
                        "title": str(item.get("song_name") or "").strip(),
                        "hash": str(item.get("hash") or "").strip(),
                        "album_id": str(item.get("album_id") or "").strip(),
                        "album_audio_id": str(
                            item.get("mixsongid")
                            or item.get("encode_album_audio_id")
                            or ""
                        ).strip(),
                    }
            except (TypeError, ValueError):
                pass
        return result

    @staticmethod
    def normalize_cover(url: str | None) -> str | None:
        if not url:
            return None
        return normalize_image_url(str(url).replace("{size}", "800"))

    @handle(
        "kugou.com",
        r"https?://(?:t1|m|www|h5)\.kugou\.com/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    )
    async def parse_kugou_music(self, searched: Match[str]):
        original_url = searched.group(0).rstrip(").,;!?，。；！？）]")
        final_url = await self.get_final_url(original_url, self.headers, retries=2)
        response = await self.http_get(
            final_url, headers=self.headers, timeout=15, retries=1
        )
        html = response.text if response.status_code < 400 else ""
        share = self.parse_share_data(html)
        parsed = urlparse(final_url)
        query = parse_qs(parsed.query)
        song_hash = share.get("hash") or (query.get("hash") or [""])[0]
        album_id = share.get("album_id") or (query.get("album_id") or [""])[0]
        album_audio_id = (
            share.get("album_audio_id") or (query.get("album_audio_id") or [""])[0]
        )

        official: dict[str, Any] = {}
        if song_hash:
            try:
                official = await self._json_get(
                    "https://m.kugou.com/app/i/getSongInfo.php",
                    params={
                        "cmd": "playInfo",
                        "hash": song_hash,
                        **({"album_id": album_id} if album_id else {}),
                        **(
                            {"album_audio_id": album_audio_id} if album_audio_id else {}
                        ),
                    },
                )
            except ParseException:
                official = {}

        metadata = parse_open_graph(html)
        title = (
            official.get("songName")
            or share.get("title")
            or metadata["title"]
            or "酷狗音乐"
        )
        author = (
            official.get("author_name")
            or official.get("singerName")
            or share.get("author")
            or "酷狗音乐"
        )
        cover = self.normalize_cover(
            (official.get("trans_param") or {}).get("union_cover")
            or official.get("album_img")
            or official.get("imgUrl")
            or metadata["image"]
        )
        contents = self._cover_contents(cover)
        audio_url = None
        if self.api_server and song_hash:
            audio_url = await self._get_api_audio_url(
                song_hash,
                album_id or str(official.get("albumid") or ""),
                album_audio_id or str(official.get("album_audio_id") or ""),
            )
        if audio_url:
            suffix = Path(urlparse(audio_url).path).suffix or ".mp3"
            contents.append(
                AudioContent(
                    self.downloader.download_audio(
                        audio_url,
                        audio_name=f"kugou_{song_hash}{suffix}",
                        max_size_mb=int(
                            self.config.get("performance", {}).get(
                                "source_max_size", 90
                            )
                        ),
                    )
                )
            )

        info = (
            f"已通过配置的酷狗 API 获取 {self.quality} 音源。"
            if audio_url
            else "已获取酷狗官方元数据；配置自建酷狗 API 后可按歌曲权限发送音源。"
        )
        return self.result(
            title=str(title).strip(),
            author=self.create_author(str(author).strip()),
            text=metadata["description"],
            contents=contents,
            url=final_url,
            extra=self._metadata_extra(info),
        )

    async def _get_api_audio_url(
        self, song_hash: str, album_id: str, album_audio_id: str
    ) -> str | None:
        headers = dict(self.headers)
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            register = await self.http_get(
                f"{self.api_server}/register/dev",
                headers=headers,
                timeout=15,
                retries=1,
            )
            if register.status_code >= 400:
                return None
            registered_cookie = self._cookies_from_headers(register.headers)
            if registered_cookie:
                headers["Cookie"] = self._merge_cookies(
                    headers.get("Cookie", ""), registered_cookie
                )
            response = await self.http_get(
                f"{self.api_server}/song/url",
                params={
                    "hash": song_hash,
                    "free_part": 0,
                    "quality": self.quality,
                    **({"album_id": album_id} if album_id else {}),
                    **({"album_audio_id": album_audio_id} if album_audio_id else {}),
                },
                headers=headers,
                timeout=15,
                retries=1,
            )
            if response.status_code >= 400:
                return None
            data = response.json()
        except Exception:
            return None
        return _first_audio_url(data)

    @staticmethod
    def _merge_cookies(*values: str) -> str:
        cookies: dict[str, str] = {}
        for value in values:
            for part in (value or "").split(";"):
                if "=" not in part:
                    continue
                name, item = part.strip().split("=", 1)
                if name:
                    cookies[name] = item
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    @classmethod
    def _cookies_from_headers(cls, headers: Any) -> str:
        values = []
        if hasattr(headers, "get_list"):
            values = headers.get_list("set-cookie")
        if not values:
            raw = headers.get("set-cookie", "") if headers else ""
            values = re.split(r",(?=\s*[^;,=]+=[^;,]+)", raw) if raw else []
        pairs = []
        for value in values:
            pair = str(value).split(";", 1)[0].strip()
            if "=" in pair:
                pairs.append(pair)
        return cls._merge_cookies(*pairs)


__all__ = [
    "KugouMusicParser",
    "QQMusicParser",
    "parse_open_graph",
]
