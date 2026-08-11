from __future__ import annotations

import random
import time
from re import Match
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlparse

from ..data import ImageContent, Platform
from ..exception import ParseException
from .base import BaseParser, handle


class WeixinChannelParser(BaseParser):
    platform: ClassVar[Platform] = Platform(
        name="weixin_channel", display_name="微信视频号"
    )
    parse_url = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
    feed_url = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"

    parse_headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": "https://yuanbao.tencent.com",
        "referer": "https://yuanbao.tencent.com/chat/naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "t-userid": "b9575f6b0a8c4a55a08096904a5ef20a",
        "x-agentid": "naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
        "x-commit-tag": "72282a0d",
        "x-device-id": "1921b001708100d7fa31002b9646bd0cc15a3e2e1f",
        "x-hy106": "",
        "x-hy92": "e963067ffa31002b9646bd0c03000008b1951a",
        "x-hy93": "1921b001708100d7fa31002b9646bd0cc15a3e2e1f",
        "x-id": "b9575f6b0a8c4a55a08096904a5ef20a",
        "x-instance-id": "5",
        "x-language": "zh-CN",
        "x-platform": "mac",
        "x-requested-with": "XMLHttpRequest",
        "x-source": "web",
        "x-web-third-source": "main",
        "x-webdriver": "0",
        "x-webversion": "2.69.0",
    }
    feed_headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        self.cookie = config.get("cookies", {}).get("yuanbao_cookie", "")

    @staticmethod
    def generate_rid(now: int | None = None, random_hex: str | None = None) -> str:
        now = now or int(time.time())
        random_hex = random_hex or "".join(
            random.choice("0123456789abcdef") for _ in range(8)
        )
        return f"{now:x}-{random_hex}"

    @staticmethod
    def extract_feed_credentials(playable_url: str) -> tuple[str, str]:
        query = parse_qs(urlparse(playable_url or "").query)
        token = (query.get("token") or [""])[0]
        export_id = (query.get("eid") or [""])[0]
        return token, export_id

    @staticmethod
    def clean_video_url(video_url: str) -> str:
        try:
            parsed = urlparse(video_url)
            query = parse_qs(parsed.query)
            file_key = (query.get("encfilekey") or [""])[0]
            token = (query.get("token") or [""])[0]
            if file_key and token:
                return (
                    f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    f"?encfilekey={quote(file_key)}&token={quote(token)}"
                )
        except Exception:
            pass
        return video_url

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            response = await self.client.post(
                url,
                json=payload,
                headers=headers,
                timeout=15,
                verify=False,
            )
        except Exception as exc:
            raise ParseException(f"视频号接口连接失败: {exc}") from exc
        if response.status_code >= 400:
            raise ParseException(f"视频号接口请求失败: HTTP {response.status_code}")
        try:
            data = response.json()
        except Exception as exc:
            raise ParseException("视频号接口返回了无效数据") from exc
        if not isinstance(data, dict):
            raise ParseException("视频号接口返回格式异常")
        return data

    async def _parse_share(self, share_url: str) -> dict[str, Any]:
        headers = {**self.parse_headers, "cookie": self.cookie}
        result = await self._post_json(
            self.parse_url,
            {"type": "video_channel_url", "url": share_url, "scene": 1},
            headers,
        )
        data = result.get("data") or {}
        if not data.get("wx_export_id"):
            raise ParseException("元宝接口未返回视频号导出信息，Cookie 可能已失效")
        token, export_id = self.extract_feed_credentials(data.get("playable_url", ""))
        if not token or not export_id:
            raise ParseException("元宝接口返回的播放链接缺少 token 或 eid")
        return data | {"_token": token, "_export_id": export_id}

    async def _get_feed(self, token: str, export_id: str) -> dict[str, Any]:
        rid = self.generate_rid()
        page_url = "https://channels.weixin.qq.com/finder-preview/pages/feed"
        url = f"{self.feed_url}?_rid={rid}&_pageUrl={quote(page_url, safe='')}"
        referer = (
            f"{page_url}?entry_card_type=48&comment_scene=39&appid=0"
            f"&token={quote(token, safe='')}&entry_scene=0&eid={quote(export_id, safe='')}"
        )
        result = await self._post_json(
            url,
            {"baseReq": {"generalToken": token}, "exportId": export_id},
            {**self.feed_headers, "Referer": referer},
        )
        if result.get("errCode") not in (None, 0):
            raise ParseException(
                f"视频号接口返回错误: {result.get('errMsg') or result['errCode']}"
            )
        data = result.get("data") or {}
        if not data.get("feedInfo") and not data.get("authorInfo"):
            raise ParseException("视频号分享链接已失效或预览凭证已过期")
        return data

    @handle(
        "weixin.qq.com/sph",
        r"https?://weixin\.qq\.com/sph/[A-Za-z0-9]+[^\s<>]*",
    )
    async def parse_weixin_channel(self, searched: Match[str]):
        share_url = searched.group(0).rstrip(").,;!?，。；！？）]")
        if not self.cookie:
            raise ParseException(
                "微信视频号解析需要在插件配置中填写腾讯元宝 Web Cookie"
            )
        parse_data = await self._parse_share(share_url)
        data = await self._get_feed(parse_data["_token"], parse_data["_export_id"])
        feed = data.get("feedInfo") or {}
        author_data = data.get("authorInfo") or {}
        video_url = feed.get("videoUrl") or (feed.get("h264VideoInfo") or {}).get(
            "videoUrl"
        )
        cover = feed.get("coverUrl") or parse_data.get("cover_url")
        contents = []
        if video_url:
            contents.append(
                self.create_video_content(
                    video_url,
                    cover_url=cover,
                    ext_headers={"Referer": "https://channels.weixin.qq.com/"},
                )
            )
        elif cover:
            contents.append(
                ImageContent(
                    self.downloader.download_img(
                        cover,
                        ext_headers={"Referer": "https://channels.weixin.qq.com/"},
                    )
                )
            )
        stats = [
            f"点赞：{feed.get('likeCountFmt')}" if feed.get("likeCountFmt") else None,
            f"收藏：{feed.get('favCountFmt')}" if feed.get("favCountFmt") else None,
            f"评论：{feed.get('commentCountFmt')}"
            if feed.get("commentCountFmt")
            else None,
            f"转发：{feed.get('forwardCountFmt')}"
            if feed.get("forwardCountFmt")
            else None,
        ]
        description = feed.get("description") or parse_data.get("desc") or ""
        text = "\n".join(
            item for item in (description, " · ".join(x for x in stats if x)) if item
        )
        timestamp = feed.get("createtime")
        try:
            timestamp = int(timestamp) if timestamp else None
        except (TypeError, ValueError):
            timestamp = None
        return self.result(
            title="微信视频号",
            author=self.create_author(
                author_data.get("nickname") or parse_data.get("author") or "视频号作者",
                author_data.get("headImgUrl") or parse_data.get("author_icon"),
            ),
            text=text or None,
            contents=contents,
            timestamp=timestamp,
            url=share_url,
            extra={"send_text": True},
        )


__all__ = ["WeixinChannelParser"]
