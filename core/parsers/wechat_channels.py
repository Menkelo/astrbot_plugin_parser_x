"""Bounded public WeChat Channels share parser; optional Yuanbao session assist."""
from __future__ import annotations

import json
import random
import re
from urllib.parse import parse_qs, quote, urlsplit

from ..data import Author, Platform
from ..exception import ParseException
from .base import BaseParser, handle


class WechatChannelsParser(BaseParser):
    platform = Platform("wechat_channels", "微信视频号")
    FEED_INFO_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
    YUANBAO_API = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        cookies = config.get("cookies", {}) if hasattr(config, "get") else {}
        raw = cookies.get("wechat_channels_cookie", "") if isinstance(cookies, dict) else ""
        self.yuanbao_cookie = self._normalize_cookie(raw)

    @staticmethod
    def _normalize_cookie(raw):
        # Allow copied line breaks between cookie pairs, never arbitrary headers
        # or control characters. Validate before stripping so tabs cannot hide.
        if not isinstance(raw, str) or any(ord(c) < 32 and c not in "\r\n" or ord(c) == 127 for c in raw):
            return ""
        value = re.sub(r"[\r\n]+", " ", raw).strip(" ")
        pairs = []
        for part in value.split(";"):
            part = part.strip(" ")
            if not part:
                continue
            if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+=[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]*", part):
                return ""
            pairs.append(part)
        return "; ".join(pairs)

    @classmethod
    def _share_id(cls, url: str) -> str:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"channels.weixin.qq.com", "weixin.qq.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            raise ParseException("微信视频号仅接受受支持的 HTTPS 分享链接")
        path_match = re.fullmatch(r"/(?:sph/|s/)([A-Za-z0-9_-]{4,128})/?", parsed.path)
        if not path_match and not (parsed.hostname == "channels.weixin.qq.com" and parsed.path == "/finder-preview/pages/sph"):
            raise ParseException("微信视频号分享路径不受支持")
        share_id = path_match.group(1) if path_match else parse_qs(parsed.query).get("id", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", share_id or ""):
            raise ParseException("无法从视频号分享链接提取作品标识")
        return share_id

    @handle("weixin.qq.com/sph/", r"https://weixin\.qq\.com/sph/[A-Za-z0-9_-]{4,128}/?(?:\?[^\s<>]*)?")
    @handle("channels.weixin.qq.com", r"https://channels\.weixin\.qq\.com/(?:(?:sph/|s/)[A-Za-z0-9_-]{4,128}/?(?:\?[^\s<>]*)?|finder-preview/pages/sph\?[^\s<>]+)")
    async def parse_share(self, searched):
        url = searched.group(0).rstrip("/")
        share_id = self._share_id(url)
        try:
            result = await self._feed_info(
                {"baseReq": {"generalToken": ""}, "shortUri": share_id},
                referer=f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={quote(share_id, safe='')}",
                page_url="https://channels.weixin.qq.com/finder-preview/pages/sph",
            )
            return self._build_result(result, share_id, url)
        except Exception as exc:
            if not self.yuanbao_cookie:
                raise ParseException("微信视频号公开接口不可用，可配置元宝 Cookie 启用备用解析") from exc
        try:
            result = await self._parse_yuanbao(url, share_id)
            return self._build_result(result, share_id, url)
        except Exception as exc:
            raise ParseException("微信视频号公开接口和元宝备用解析均不可用") from exc

    async def _parse_yuanbao(self, url, share_id):
        response = await self.http_post(self.YUANBAO_API,
            json={"type": "video_channel_url", "url": url, "scene": 1},
            headers={"Accept": "application/json, text/plain, */*", "Content-Type": "application/json",
                "Origin": "https://yuanbao.tencent.com", "Referer": "https://yuanbao.tencent.com/chat",
                "User-Agent": self.USER_AGENT, "Cookie": self.yuanbao_cookie, "x-source": "web"},
            timeout=20, allow_redirects=False)
        data = response.json()
        if not isinstance(data, dict) or data.get("error") or data.get("code") != 0:
            raise ParseException("元宝解析返回不可用数据")
        playable = ((data.get("data") or {}).get("playable_url") if isinstance(data, dict) else None)
        if not isinstance(playable, str):
            raise ParseException("元宝未返回视频号临时播放信息")
        parsed = urlsplit(playable)
        if (parsed.scheme != "https" or parsed.hostname != "channels.weixin.qq.com"
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            raise ParseException("元宝返回了不受支持的视频号地址")
        query = parse_qs(parsed.query)
        token, eid = query.get("token", [""])[0], query.get("eid", [""])[0]
        if not token or not eid:
            raise ParseException("元宝响应缺少视频号临时凭证")
        referer = f"https://channels.weixin.qq.com/finder-preview/pages/feed?token={quote(token, safe='')}&eid={quote(eid, safe='')}&entry_card_type=48&comment_scene=39&appid=0&entry_scene=0"
        return await self._feed_info({"baseReq": {"generalToken": token}, "exportId": eid}, referer=referer,
            page_url="https://channels.weixin.qq.com/finder-preview/pages/feed")

    @staticmethod
    def _feed_error_is_success(value):
        # Legacy responses omit errMsg or use an empty string. The observed
        # object form is successful only with the explicit integer type zero.
        return (value is None or value == "" or
                isinstance(value, dict) and set(value) == {"type"}
                and type(value["type"]) is int and value["type"] == 0)

    async def _feed_info(self, payload, *, referer, page_url):
        request_id = f"{random.randrange(16**8):08x}"
        url = f"{self.FEED_INFO_API}?_rid={request_id}&_pageUrl={quote(page_url, safe='')}"
        headers = {"Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", "Content-Type": "application/json",
            "Origin": "https://channels.weixin.qq.com", "Referer": referer, "User-Agent": self.USER_AGENT}
        response = await self.http_post(url, json=payload, headers=headers,
            timeout=20, allow_redirects=False)
        result = response.json()
        if (not isinstance(result, dict) or type(result.get("errCode")) is not int
                or result["errCode"] != 0):
            raise ParseException("微信视频号公开接口返回不可用数据")
        data = result.get("data")
        if not isinstance(data, dict) or not self._feed_error_is_success(data.get("errMsg")):
            raise ParseException("微信视频号作品不可用或受访问限制")
        return result

    def _build_result(self, result, share_id, url):
        if (not isinstance(result, dict) or type(result.get("errCode")) is not int
                or result["errCode"] != 0):
            raise ParseException("微信视频号公开接口返回不可用数据")
        data = result.get("data") or {}
        if not isinstance(data, dict) or not self._feed_error_is_success(data.get("errMsg")):
            raise ParseException("微信视频号作品不可用或受访问限制")
        feed, author = data.get("feedInfo") or {}, data.get("authorInfo") or {}
        if not isinstance(feed, dict) or not isinstance(author, dict):
            raise ParseException("微信视频号响应结构无效")
        feed_id = str(feed.get("feedId") or feed.get("id") or share_id)
        video = feed.get("videoUrl") or (feed.get("h264VideoInfo") or {}).get("videoUrl") or (feed.get("h265VideoInfo") or {}).get("videoUrl")
        cover = feed.get("coverUrl")
        title = feed.get("title")
        description = feed.get("description") or feed.get("desc")
        images = []
        for item in feed.get("picInfo") or []:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                images.append(item["url"])
        contents = []
        if video:
            contents.append(self.create_video_content(video, cover, ext_headers={"User-Agent": self.USER_AGENT}))
        if images:
            contents.extend(self.create_image_contents(images, ext_headers={"User-Agent": self.USER_AGENT}))
        if not video and not images:
            raise ParseException("视频号作品没有可用视频或图片")
        name = author.get("nickname") or author.get("nickName") or "视频号作者"
        avatar_url = author.get("headImgUrl") or author.get("avatar") or ""
        avatar = self.create_author(str(name), avatar_url, ext_headers={"User-Agent": self.USER_AGENT})
        return self.result(title=title or description, text=description, author=avatar, url=url, contents=contents,
            extra={"suppress_comments": True, "video_channel_id": share_id})

    async def http_post(self, url, **kwargs):
        # Redirects are disabled; secrets only go to the exact configured Yuanbao origin.
        parsed = urlsplit(url)
        allowed = ((parsed.scheme == "https" and parsed.hostname == "channels.weixin.qq.com" and parsed.path == "/finder-preview/api/feed/get_feed_info")
                   or (parsed.scheme == "https" and parsed.hostname == "yuanbao.tencent.com" and parsed.path == "/api/weixin/get_parse_result"))
        if not allowed or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ParseException("视频号请求地址不允许")
        from curl_cffi.requests import AsyncSession
        from ..utils import maybe_close_session
        session = None
        try:
            session = AsyncSession(impersonate="chrome120", timeout=20, verify=True, discard_cookies=True)
            kwargs.pop("allow_redirects", None)
            kwargs.pop("verify", None)
            kwargs.pop("cookies", None)
            headers = {key: value for key, value in (kwargs.pop("headers", None) or {}).items()
                       if key.lower() != "cookie"}
            if parsed.hostname == "yuanbao.tencent.com" and self.yuanbao_cookie:
                headers["Cookie"] = self.yuanbao_cookie
            kwargs["headers"] = headers
            response = await session.post(url, timeout=min(20, max(1, float(kwargs.pop("timeout", 20)))), allow_redirects=False, **kwargs)
            accepted_statuses = {200, 201} if parsed.hostname == "channels.weixin.qq.com" else {200}
            if response.status_code not in accepted_statuses:
                raise ParseException("视频号接口请求失败")
            return response
        except Exception:
            raise ParseException("视频号接口请求失败") from None
        finally:
            if session is not None:
                try:
                    await maybe_close_session(session)
                except Exception:
                    raise ParseException("视频号接口请求失败") from None
