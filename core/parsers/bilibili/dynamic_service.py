import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

from bilibili_api.dynamic import Dynamic
from bilibili_api.exceptions import ResponseCodeException

from astrbot.api import logger

from ...data import ImageContent, MediaContent
from ...utils import image_to_data_uri
from ..base import ParseException


class BiliDynamicService:
    def __init__(self, parser):
        self.parser = parser
        self._img_data_uri_cache: dict[str, str | None] = {}

    # region 通用工具

    @staticmethod
    def norm_img(url: str | None) -> str | None:
        if not url:
            return None

        url = str(url).strip()
        if not url:
            return None

        if url.startswith("//"):
            return "https:" + url

        if url.startswith("http://"):
            return "https://" + url[len("http://") :]

        return url

    @staticmethod
    def norm_ts(ts) -> int | None:
        """
        B站部分接口可能返回秒级时间戳，也可能返回毫秒级时间戳。
        统一修正为秒级。
        """
        try:
            ts = int(ts)
            if ts <= 0:
                return None
            if ts > 10_000_000_000:
                ts = ts // 1000
            return ts
        except Exception:
            return None

    @staticmethod
    def fmt_time(ts: int | None) -> str | None:
        """
        展示时间，不显示秒。
        """
        if not ts:
            return None
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
        except Exception:
            return None

    @staticmethod
    def neutralize_at_text(text: str) -> str:
        if not text:
            return text
        return text.replace("@", "＠")

    @staticmethod
    def extract_rich_text_nodes_text(nodes) -> str:
        """
        从 B站 rich_text_nodes / paragraph nodes 中提取纯文本。
        """
        if not isinstance(nodes, list):
            return ""

        parts: list[str] = []

        for n in nodes:
            if not isinstance(n, dict):
                continue

            text = n.get("text")
            if isinstance(text, str) and text:
                parts.append(text)

            word = n.get("word")
            if isinstance(word, dict):
                words = word.get("words")
                if isinstance(words, str) and words:
                    parts.append(words)

        return "".join(parts).strip()

    @classmethod
    def extract_opus_paragraph_text(cls, opus: dict) -> str:
        """
        从 opus.content.paragraphs 里提取正文。
        """
        if not isinstance(opus, dict):
            return ""

        content = opus.get("content") or {}
        paragraphs = content.get("paragraphs") or []

        if not isinstance(paragraphs, list):
            return ""

        parts: list[str] = []

        for para in paragraphs:
            if not isinstance(para, dict):
                continue

            text_obj = para.get("text") or {}
            nodes = text_obj.get("nodes") or []
            text = cls.extract_rich_text_nodes_text(nodes)

            if text:
                parts.append(text)

        return "\n\n".join(parts).strip()

    @classmethod
    def extract_dynamic_title(cls, item: dict, modules: dict) -> str | None:
        """
        稳定提取 B站动态标题。

        优先级：
        1. item.basic.title
        2. module_dynamic.major.opus.title
        3. module_dynamic.major.archive.title
        4. module_dynamic.major.article.title
        """
        if isinstance(item, dict):
            basic = item.get("basic") or {}
            if isinstance(basic, dict):
                title = basic.get("title")
                if isinstance(title, str) and title.strip():
                    return title.strip()

        if not isinstance(modules, dict):
            return None

        md = modules.get("module_dynamic") or {}
        if not isinstance(md, dict):
            return None

        major = md.get("major") or {}
        if not isinstance(major, dict):
            return None

        opus = major.get("opus") or {}
        if isinstance(opus, dict):
            title = opus.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()

        archive = major.get("archive") or {}
        if isinstance(archive, dict):
            title = archive.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()

        article = major.get("article") or {}
        if isinstance(article, dict):
            title = article.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()

        return None

    @classmethod
    def extract_dynamic_text(cls, modules: dict) -> str:
        """
        稳定提取 B站动态正文。

        优先级：
        1. module_dynamic.desc.text
        2. module_dynamic.desc.rich_text_nodes
        3. module_dynamic.major.opus.summary.text
        4. module_dynamic.major.opus.summary.rich_text_nodes
        5. module_dynamic.major.opus.content.paragraphs
        6. module_dynamic.major.archive.desc
        7. module_dynamic.major.archive.title
        """
        if not isinstance(modules, dict):
            return "（无正文）"

        md = modules.get("module_dynamic") or {}
        if not isinstance(md, dict):
            return "（无正文）"

        desc = md.get("desc") or {}
        if isinstance(desc, dict):
            text = desc.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

            nodes_text = cls.extract_rich_text_nodes_text(desc.get("rich_text_nodes") or [])
            if nodes_text:
                return nodes_text

        major = md.get("major") or {}
        if isinstance(major, dict):
            opus = major.get("opus") or {}
            if isinstance(opus, dict):
                summary = opus.get("summary") or {}
                if isinstance(summary, dict):
                    summary_text = summary.get("text")
                    if isinstance(summary_text, str) and summary_text.strip():
                        return summary_text.strip()

                    summary_nodes_text = cls.extract_rich_text_nodes_text(
                        summary.get("rich_text_nodes") or []
                    )
                    if summary_nodes_text:
                        return summary_nodes_text

                paragraph_text = cls.extract_opus_paragraph_text(opus)
                if paragraph_text:
                    return paragraph_text

            archive = major.get("archive") or {}
            if isinstance(archive, dict):
                archive_desc = archive.get("desc")
                if isinstance(archive_desc, str) and archive_desc.strip():
                    return archive_desc.strip()

                archive_title = archive.get("title")
                if isinstance(archive_title, str) and archive_title.strip():
                    return archive_title.strip()

        return "（无正文）"

    @classmethod
    def extract_dynamic_image_urls(cls, modules: dict) -> list[str]:
        image_urls: list[str] = []

        if not isinstance(modules, dict):
            return image_urls

        md = modules.get("module_dynamic") or {}
        if not isinstance(md, dict):
            return image_urls

        major = md.get("major") or {}
        if not isinstance(major, dict):
            return image_urls

        opus = major.get("opus") or {}
        if isinstance(opus, dict):
            pics = opus.get("pics") or []
            if isinstance(pics, list):
                for p in pics:
                    if isinstance(p, dict):
                        u = p.get("url")
                        if isinstance(u, str) and u:
                            image_urls.append(u)

            content = opus.get("content") or {}
            paragraphs = content.get("paragraphs") or []
            if isinstance(paragraphs, list):
                for para in paragraphs:
                    if not isinstance(para, dict):
                        continue

                    pic_obj = para.get("pic") or {}
                    pics2 = pic_obj.get("pics") or []
                    if isinstance(pics2, list):
                        for p in pics2:
                            if isinstance(p, dict):
                                u = p.get("url")
                                if isinstance(u, str) and u:
                                    image_urls.append(u)

        archive = major.get("archive") or {}
        if isinstance(archive, dict):
            cover = archive.get("cover")
            if isinstance(cover, str) and cover:
                image_urls.append(cover)

        seen = set()
        uniq = []

        for u in image_urls:
            u = cls.norm_img(u)
            if not u:
                continue
            if u not in seen:
                seen.add(u)
                uniq.append(u)

        return uniq

    # endregion

    async def img_to_data_uri(
        self,
        img_url: str | None,
        *,
        referer: str = "https://www.bilibili.com/",
        max_bytes: int = 4 * 1024 * 1024,
    ) -> str | None:
        """
        将 B站图片转 data URI，避免 Playwright 渲染时头像加载失败。
        """
        img_url = self.norm_img(img_url)
        if not img_url:
            return None

        if img_url in self._img_data_uri_cache:
            return self._img_data_uri_cache[img_url]

        if len(self._img_data_uri_cache) > 512:
            self._img_data_uri_cache.clear()

        headers = self.parser.headers.copy()
        headers["Cache-Control"] = "no-cache"
        data_uri = await image_to_data_uri(
            self.parser.http_get,
            img_url,
            headers=headers,
            referer=referer,
            normalizer=None,
            max_bytes=max_bytes,
            timeout=10,
            debug_label="[Bilibili] dynamic image",
        )
        self._img_data_uri_cache[img_url] = data_uri
        return data_uri

    async def _fetch_dynamic_raw(self, dynamic_id: int) -> dict:
        raw_dynamic = None
        last_err: Exception | None = None

        for i in range(3):
            try:
                dynamic = Dynamic(dynamic_id, await self.parser.credential)
                raw_dynamic = await dynamic.get_info()
                break
            except ResponseCodeException as e:
                last_err = e
                if getattr(e, "code", None) in (-352, -412, -799):
                    await asyncio.sleep(0.8 * (i + 1))
                    continue
                raise ParseException(f"B站动态解析失败: {e}") from e
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.6 * (i + 1))

        if raw_dynamic is not None:
            return raw_dynamic

        try:
            api = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
            headers = self.parser.headers.copy()
            headers["Referer"] = f"https://t.bilibili.com/{dynamic_id}"

            if self.parser.bili_ck:
                headers["Cookie"] = self.parser.bili_ck

            resp = await self.parser.http_get(
                api,
                headers=headers,
                params={"id": dynamic_id, "timezone_offset": -480},
                allow_redirects=True,
                timeout=10,
            )

            try:
                data = resp.json()
            except Exception:
                data = {}

            if not isinstance(data, dict) or data.get("code") != 0:
                raise ParseException(
                    f"B站动态解析失败: {data.get('message') or data.get('msg') or data.get('code')}"
                )

            item = ((data.get("data") or {}).get("item")) or {}
            if not item:
                raise ParseException("B站动态解析失败: detail 数据为空")

            return {"item": item}

        except Exception as e:
            raise ParseException(
                f"B站动态解析失败（疑似风控 -352），建议配置 bili_ck 后重试: {e or last_err}"
            ) from (e if isinstance(e, Exception) else last_err)

    async def parse_dynamic(self, dynamic_id: int):
        raw_dynamic = await self._fetch_dynamic_raw(dynamic_id)

        item = (raw_dynamic or {}).get("item") or {}
        modules = item.get("modules") or {}

        module_author = modules.get("module_author") or {}
        author_name = module_author.get("name") or "B站用户"
        author_avatar = self.norm_img(module_author.get("face"))

        pub_ts = self.norm_ts(module_author.get("pub_ts"))
        time_text = self.fmt_time(pub_ts)

        dynamic_title = self.extract_dynamic_title(item, modules)
        full_text = self.neutralize_at_text(self.extract_dynamic_text(modules))
        image_urls = self.extract_dynamic_image_urls(modules)

        full_images: list[MediaContent] = (
            self.parser.create_image_contents(image_urls) if image_urls else []
        )

        author_avatar_data_uri = await self.img_to_data_uri(
            author_avatar,
            referer=f"https://t.bilibili.com/{dynamic_id}",
        )

        digest = hashlib.md5(
            (
                f"{dynamic_id}|{author_name}|{author_avatar}|"
                f"{dynamic_title}|{full_text}|{image_urls}|"
                f"dyn_service_v3"
            ).encode()
        ).hexdigest()[:10]

        out_path = Path(self.parser.cache_dir) / f"bili_dynamic_{dynamic_id}_{digest}.png"

        if not out_path.exists():
            await self.parser.dynamic_renderer.render_dynamic_card(
                out_path=out_path,
                author_name=author_name,
                author_avatar=author_avatar_data_uri or author_avatar,
                title=dynamic_title,
                text=full_text,
                timestamp_text=time_text,
            )

        contents: list[MediaContent] = [ImageContent(out_path)]
        contents.extend(full_images)

        return self.parser.result(
            title=dynamic_title,
            contents=contents,
            text=full_text,
            author=self.parser.create_author(author_name, author_avatar),
            timestamp=pub_ts,
            url=f"https://t.bilibili.com/{dynamic_id}",
            # 不要 force_direct_media，否则图文动态可能不会走合并转发
            extra={},
        )
