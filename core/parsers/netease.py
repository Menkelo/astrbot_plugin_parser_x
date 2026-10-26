"""Independent anonymous NetEase single-song parser; no unlock or fallback source."""
from __future__ import annotations

from ..card_flow import build_card_extra
from ..data import Author, Platform
from .base import BaseParser, ParseException, handle


class NeteaseParser(BaseParser):
    platform = Platform("netease", "网易云音乐")

    @handle("music.163.com", r"https?://music\.163\.com/(?:\#/)?(?:m/)?song\?[^\s<>]*")
    async def parse_song(self, searched):
        from urllib.parse import parse_qs
        query = searched.group(0).split("?", 1)[1]
        ids = parse_qs(query).get("id", [])
        if len(ids) != 1 or not ids[0].isascii() or not ids[0].isdigit() or int(ids[0]) <= 0:
            raise ParseException("网易云歌曲 ID 无效")
        song_id = str(int(ids[0]))
        payload = await self._request_json("/api/song/detail/", {"ids": f"[{song_id}]"})
        songs = payload.get("songs") or []
        song = next((item for item in songs if isinstance(item, dict) and str(item.get("id")) == song_id), None)
        if payload.get("code") != 200 or not song:
            raise ParseException("网易云歌曲详情不可用或身份不匹配")
        album = song.get("album") or song.get("al") or {}
        artists = song.get("artists") or song.get("ar") or []
        author = " / ".join(str(item.get("name")) for item in artists if isinstance(item, dict) and item.get("name"))
        extra = build_card_extra([], cover=self._media_url(album.get("picUrl")))
        extra["audio_status"] = "unavailable"
        extra["info"] = "音频暂不可用（权限、地区或接口限制）；未解锁或寻找替代音源"
        # Cards must never consume AudioContent: shared delivery sends it as File.
        extra["delivery_text_card_consume_non_video"] = False
        contents = []
        try:
            audio_payload = await self._request_json("/api/song/enhance/player/url/v1", {
                "ids": f"[{song_id}]", "level": "standard", "encodeType": "mp3",
            })
            for audio in audio_payload.get("data") or []:
                if not isinstance(audio, dict) or str(audio.get("id")) != song_id:
                    continue
                url = self._media_url(audio.get("url"))
                if audio_payload.get("code") != 200 or audio.get("code") != 200 or not url:
                    break
                duration = float(song.get("duration") or song.get("dt") or 0) / 1000
                trial = audio.get("freeTrialInfo")
                if isinstance(trial, dict) and trial:
                    # Trial boundaries are seconds; song.duration/dt above are milliseconds.
                    duration = max(0, float(trial.get("end", 0)) - float(trial.get("start", 0)))
                    extra["audio_status"] = "preview"
                    extra["info"] = "仅官方试听片段（非完整音频）"
                else:
                    extra["audio_status"] = "available"
                    extra["info"] = "官方匿名接口提供的可用音频"
                contents.append(self.create_audio_content(url, duration))
                break
        except Exception:
            # Permission, region, malformed/changed API: detail remains useful.
            pass
        from ..comment_settings import CommentSettings
        from .netease_comment import NeteaseCommentFeed
        settings = CommentSettings.from_config(self.config, "netease")
        if settings.enabled:
            feed = NeteaseCommentFeed(self, limit=settings.display_count)
            extra["text_card_comment_factory"] = lambda: feed.fetch_document(
                song_id, str(song.get("name") or ""), str(extra.get("text_card_media") or ""))
            extra["comment_timeout"] = settings.timeout
        return self.result(title=song.get("name"), author=Author(author or "未知歌手"),
                           text=str(album.get("name") or ""), contents=contents,
                           url=f"https://music.163.com/song?id={song_id}", extra=extra)

    async def _request_json(self, path, params=None):
        import json
        import re
        import aiohttp
        from urllib.parse import urlsplit
        if not (path in {"/api/song/detail/", "/api/song/enhance/player/url/v1"}
                or re.fullmatch(r"/api/v1/resource/hotcomments/R_SO_4_[1-9][0-9]*", path)):
            raise ParseException("网易云 API 路径不允许")
        url = "https://music.163.com" + path
        # Never reuse user cookies or globally configured proxy authentication.
        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:
                async with session.get(url, params=params, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=12),
                        headers={"Referer": "https://music.163.com/", "User-Agent": "Mozilla/5.0"}) as response:
                    final = urlsplit(str(response.url))
                    if response.status != 200 or final.scheme != "https" or final.hostname != "music.163.com" or final.path != path:
                        raise ParseException("网易云 API 响应身份或状态异常")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise ParseException("网易云 API 响应过大")
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise ParseException("网易云 API 响应格式异常")
                    return payload
        except ParseException:
            raise
        except Exception as error:
            raise ParseException("网易云 API 匿名请求失败") from error

    @staticmethod
    def _media_url(value):
        from urllib.parse import urlsplit, urlunsplit
        if not isinstance(value, str):
            return ""
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                    or parsed.port not in {None, 80, 443} or not host.endswith(".music.126.net")):
                return ""
            return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
        except ValueError:
            return ""
