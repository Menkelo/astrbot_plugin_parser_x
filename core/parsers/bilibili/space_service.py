import asyncio
import hashlib
import json
import random
import time
from pathlib import Path

from astrbot.api import logger

from ...data import ImageContent
from ...utils import image_to_data_uri
from ..base import ParseException


class BiliSpaceService:
    EP_CARD = "https://api.bilibili.com/x/web-interface/card"
    EP_REL = "https://api.bilibili.com/x/relation/stat"
    EP_TOP_ARC = "https://api.bilibili.com/x/space/top/arc"
    EP_ARC_SEARCH = "https://api.bilibili.com/x/space/arc/search"

    def __init__(self, parser):
        self.parser = parser
        perf = parser.config.get("performance", {})
        self._ttl = int(perf.get("bili_space_cache_ttl", 180))
        self._rep_cache: dict[int, tuple[float, dict | None]] = {}

        # 图片 data URI 缓存，避免头像/封面重复下载
        self._img_data_uri_cache: dict[str, str | None] = {}

    def _headers(self, mid: int) -> dict[str, str]:
        headers = self.parser.headers.copy()
        headers.update(
            {
                "Referer": f"https://space.bilibili.com/{mid}",
                "Origin": "https://space.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Cache-Control": "no-cache",
            }
        )

        if self.parser.bili_ck:
            headers["Cookie"] = self.parser.bili_ck

        return headers

    async def _get_json(
        self,
        url: str,
        params: dict,
        mid: int,
        timeout: int = 8,
        retry: int = 3,
    ) -> dict:
        """
        B站空间 API 请求。

        对 -799 / -352 / -412 做退避重试，降低偶发风控影响。
        """
        headers = self._headers(mid)
        last: dict = {}

        for i in range(retry):
            try:
                resp = await self.parser.http_get(
                    url,
                    headers=headers,
                    params=params,
                    allow_redirects=True,
                    timeout=timeout,
                )

                try:
                    data = resp.json()
                except Exception:
                    txt = getattr(resp, "text", "") or ""
                    data = json.loads(txt) if txt else {}

                if not isinstance(data, dict):
                    data = {}

                last = data
                code = data.get("code")
                msg = data.get("message") or data.get("msg")

                logger.debug(
                    f"[Bilibili-space] api={url} try={i + 1}/{retry} code={code} msg={msg}"
                )

                if code == 0:
                    return data

                # 风控/频率限制，退避重试
                if code in (-799, -352, -412):
                    await asyncio.sleep((i + 1) * 1.2 + random.uniform(0.2, 0.8))
                    continue

                return data

            except Exception as e:
                logger.debug(f"[Bilibili-space] api={url} try={i + 1}/{retry} ex={e}")
                await asyncio.sleep((i + 1) * 0.7 + random.uniform(0.1, 0.4))

        return last

    @staticmethod
    def _norm_cover(url: str | None) -> str | None:
        if not url:
            return None

        url = str(url).strip()
        if not url:
            return None

        if url.startswith("//"):
            return f"https:{url}"

        if url.startswith("http://"):
            return "https://" + url[len("http://") :]

        return url

    @staticmethod
    def _fmt_date(ts) -> str | None:
        try:
            ts = int(ts)
            if ts <= 0:
                return None
            if ts > 10_000_000_000:
                ts = ts // 1000
            return time.strftime("%Y-%m-%d", time.localtime(ts))
        except Exception:
            return None

    @staticmethod
    def _play_val(v: dict) -> int:
        p = v.get("play", 0)
        try:
            return int(p)
        except Exception:
            return 0

    async def _img_to_data_uri(
        self,
        img_url: str | None,
        *,
        mid: int,
        max_bytes: int = 6 * 1024 * 1024,
    ) -> str | None:
        """
        将 B站头像/封面转 data URI，避免 Playwright 直接加载远程图失败。
        """
        img_url = self._norm_cover(img_url)
        if not img_url:
            return None

        if img_url in self._img_data_uri_cache:
            return self._img_data_uri_cache[img_url]

        if len(self._img_data_uri_cache) > 512:
            self._img_data_uri_cache.clear()

        referer = f"https://space.bilibili.com/{mid}"
        data_uri = await image_to_data_uri(
            self.parser.http_get,
            img_url,
            headers=self._headers(mid),
            referer=referer,
            normalizer=None,
            max_bytes=max_bytes,
            timeout=12,
            debug_label="[Bilibili-space] image",
        )
        self._img_data_uri_cache[img_url] = data_uri
        return data_uri

    def _to_work(self, v: dict) -> dict | None:
        if not isinstance(v, dict):
            return None

        bvid = v.get("bvid")
        aid = v.get("aid")

        url = None
        if bvid:
            url = f"https://www.bilibili.com/video/{bvid}"
        elif aid:
            url = f"https://www.bilibili.com/video/av{aid}"

        if not url:
            return None

        ts = v.get("created") or v.get("pubdate") or v.get("ctime") or 0

        return {
            "title": v.get("title") or "未命名稿件",
            "cover": self._norm_cover(v.get("pic") or v.get("cover")),
            "url": url,
            "ts": int(ts) if str(ts).isdigit() else 0,
            "date": self._fmt_date(ts),
        }

    @staticmethod
    def _cache_get(
        cache: dict[int, tuple[float, dict | None]],
        key: int,
        ttl: int,
    ) -> dict | None:
        item = cache.get(key)
        if not item:
            return None

        ts, val = item
        if time.time() - ts > ttl:
            cache.pop(key, None)
            return None

        return val

    @staticmethod
    def _cache_set(
        cache: dict[int, tuple[float, dict | None]],
        key: int,
        val: dict | None,
    ):
        cache[key] = (time.time(), val)

    async def _fetch_profile(self, mid: int) -> dict:
        profile = {
            "name": f"UP主 {mid}",
            "avatar": None,
            "sign": "",
            "level": None,
            "official_title": None,
            "following": None,
            "follower": None,
            "archive_count": None,
        }

        card = await self._get_json(self.EP_CARD, {"mid": mid}, mid, retry=3)
        if card.get("code") == 0:
            data = card.get("data") or {}
            c = data.get("card") or {}

            profile["name"] = c.get("name") or profile["name"]
            profile["avatar"] = self._norm_cover(c.get("face"))
            profile["sign"] = c.get("sign") or ""
            profile["level"] = (c.get("level_info") or {}).get("current_level")

            profile["official_title"] = (
                (c.get("Official") or {}).get("title")
                or (c.get("official") or {}).get("title")
                or None
            )

            profile["following"] = data.get("following")
            profile["follower"] = data.get("follower")
            profile["archive_count"] = data.get("archive_count")
        else:
            raise ParseException(
                f"空间信息获取失败: {card.get('message') or card.get('msg') or card.get('code')}"
            )

        rel = await self._get_json(self.EP_REL, {"vmid": mid}, mid, retry=2)
        if rel.get("code") == 0:
            d = rel.get("data") or {}

            if isinstance(d.get("following"), int):
                profile["following"] = d.get("following")

            if isinstance(d.get("follower"), int):
                profile["follower"] = d.get("follower")

        return profile

    async def _fetch_representative(self, mid: int) -> dict | None:
        """
        获取代表作：
        1. 优先置顶稿件；
        2. 置顶失败时取播放最高稿件；
        3. 风控失败时直接返回 None，不影响空间卡片。
        """
        top = await self._get_json(self.EP_TOP_ARC, {"vmid": mid}, mid, retry=3)

        if top.get("code") == 0:
            data = top.get("data") or {}

            # 有的返回 data.archive，有的直接 data 是稿件
            arc = data.get("archive") if isinstance(data.get("archive"), dict) else data
            rep = self._to_work(arc)

            if rep:
                return rep
        else:
            logger.debug(
                f"[Bilibili] top arc unavailable mid={mid}, "
                f"code={top.get('code')}, msg={top.get('message') or top.get('msg')}"
            )

        search = await self._get_json(
            self.EP_ARC_SEARCH,
            {
                "mid": mid,
                "pn": 1,
                "ps": 30,
                "tid": 0,
                "keyword": "",
                "order": "click",
                "platform": "web",
            },
            mid,
            retry=3,
        )

        if search.get("code") == 0:
            vlist = (((search.get("data") or {}).get("list") or {}).get("vlist") or [])
            if vlist:
                best = max(vlist, key=self._play_val)
                rep = self._to_work(best)
                if rep:
                    return rep

        logger.info(
            f"[Bilibili] representative not found mid={mid}, "
            f"code={search.get('code')}, msg={search.get('message') or search.get('msg')}"
        )
        return None

    async def parse_space(self, mid: int):
        profile = await self._fetch_profile(mid)

        rep = self._cache_get(self._rep_cache, mid, self._ttl)
        if rep is None:
            rep = await self._fetch_representative(mid)
            self._cache_set(self._rep_cache, mid, rep)

        # 头像提前转 data URI，解决 Playwright 头像空白问题
        avatar_data_uri = await self._img_to_data_uri(profile.get("avatar"), mid=mid)

        # 代表作封面也提前转 data URI，失败则隐藏封面图，不影响卡片
        rep_for_render = None
        if rep:
            rep_for_render = rep.copy()
            cover_data_uri = await self._img_to_data_uri(rep.get("cover"), mid=mid)
            rep_for_render["cover"] = cover_data_uri

        digest = hashlib.md5(
            (
                f"{mid}|{profile['name']}|{profile['sign']}|"
                f"{profile['following']}|{profile['follower']}|"
                f"{profile['archive_count']}|{rep}|"
                f"{bool(avatar_data_uri)}|{bool(rep_for_render and rep_for_render.get('cover'))}|"
                f"space_datauri_v3"
            ).encode()
        ).hexdigest()[:10]

        out_path = Path(self.parser.cache_dir) / f"bili_space_{mid}_{digest}.png"

        if not out_path.exists():
            await self.parser.space_renderer.render_space_card(
                out_path=out_path,
                name=profile["name"],
                mid=mid,
                avatar=avatar_data_uri,
                sign=profile["sign"],
                level=profile["level"],
                official_title=profile["official_title"],
                following=profile["following"],
                follower=profile["follower"],
                archive_count=profile["archive_count"],
                representative_work=rep_for_render,
            )

        return self.parser.result(
            contents=[ImageContent(out_path)],
            extra={"force_direct_media": True},
        )
