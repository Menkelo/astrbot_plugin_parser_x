import asyncio
import hashlib
import time
from html import unescape
from pathlib import Path
from typing import Any

from bilibili_api.dynamic import Dynamic
from bilibili_api.exceptions import ResponseCodeException

from astrbot.api import logger

from ...data import ImageContent, MediaContent
from ...utils import cached_image_to_data_uri, normalize_image_url
from ..base import ParseException


class BiliDynamicService:
    def __init__(self, parser):
        self.parser = parser
        self._img_data_uri_cache: dict[str, str | None] = {}

    # region 通用工具

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
    def _first_str(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return unescape(value.strip())
        return ""

    @staticmethod
    def _iter_modules(modules: Any) -> list[dict]:
        if not isinstance(modules, list):
            return []
        return [module for module in modules if isinstance(module, dict)]

    @classmethod
    def _module_by_type(cls, modules: Any, module_type: str) -> dict | None:
        for module in cls._iter_modules(modules):
            if module.get("module_type") == module_type:
                return module
        return None

    @classmethod
    def _dedupe_image_urls(cls, image_urls: list[str]) -> list[str]:
        seen = set()
        uniq = []

        for url in image_urls:
            url = normalize_image_url(url)
            if not url:
                continue
            if url not in seen:
                seen.add(url)
                uniq.append(url)

        return uniq

    @classmethod
    def extract_author_info(cls, modules: Any) -> tuple[str, str | None, int | None]:
        module_author: dict[str, Any] = {}

        if isinstance(modules, dict):
            module_author = modules.get("module_author") or {}
        elif isinstance(modules, list):
            for module in cls._iter_modules(modules):
                author = module.get("module_author")
                if isinstance(author, dict):
                    module_author = author
                    break

        author_name = cls._first_str(module_author.get("name")) or "B站用户"
        author_avatar = normalize_image_url(module_author.get("face"))
        pub_ts = cls.norm_ts(module_author.get("pub_ts"))
        return author_name, author_avatar, pub_ts

    @staticmethod
    def extract_rich_text_nodes_text(nodes) -> str:
        """
        从 B站 rich_text_nodes / paragraph nodes 中提取文本。

        B站 opus 会把话题、@、网页链接、公式等拆成不同 node；这里保留
        可见文本，让统一的 TextCardRenderer 负责高亮。
        """
        if not isinstance(nodes, list):
            return ""

        parts: list[str] = []

        for n in nodes:
            if not isinstance(n, dict):
                continue

            text = BiliDynamicService._first_str(n.get("text"), n.get("orig_text"))
            word = n.get("word")
            rich = n.get("rich")
            formula = n.get("formula")

            if not text and isinstance(word, dict):
                text = BiliDynamicService._first_str(
                    word.get("words"),
                    word.get("text"),
                    word.get("orig_text"),
                )

            if isinstance(rich, dict):
                rich_type = rich.get("type")
                jump_url = BiliDynamicService._first_str(
                    rich.get("jump_url"),
                    rich.get("url"),
                    rich.get("uri"),
                )
                rich_text = BiliDynamicService._first_str(
                    rich.get("text"),
                    rich.get("orig_text"),
                    rich.get("title"),
                    rich.get("desc"),
                )
                if rich_type == "RICH_TEXT_NODE_TYPE_WEB" and jump_url:
                    text = f"{rich_text or text or '网页链接'} {jump_url}"
                elif not text and jump_url:
                    text = "网页链接"
                elif not text and rich_text:
                    text = rich_text

            if not text and isinstance(formula, dict):
                text = BiliDynamicService._first_str(
                    formula.get("latex_content"),
                    formula.get("text"),
                )

            if text:
                parts.append(text)

        return "".join(parts).strip()

    @classmethod
    def extract_link_card_text(cls, link_card: Any) -> str:
        if not isinstance(link_card, dict):
            return ""

        title = cls._first_str(
            link_card.get("title"),
            link_card.get("text"),
            link_card.get("desc"),
        )
        jump_url = cls._first_str(
            link_card.get("jump_url"),
            link_card.get("url"),
            link_card.get("uri"),
        )

        if title and jump_url:
            return f"{title} {jump_url}"
        if title:
            return title
        if jump_url:
            return f"网页链接 {jump_url}"
        return ""

    @classmethod
    def extract_paragraph_text(cls, para: dict) -> str:
        if not isinstance(para, dict):
            return ""

        para_type = para.get("para_type")
        text_obj = para.get("text") or {}
        if isinstance(text_obj, dict):
            text = cls.extract_rich_text_nodes_text(text_obj.get("nodes") or [])
            if text:
                return f"> {text}" if para_type == 4 else text

        if para_type == 5:
            list_obj = para.get("list") or {}
            items = []
            if isinstance(list_obj, dict):
                items = list_obj.get("items") or []
            lines: list[str] = []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    nodes = item.get("nodes") or (item.get("text") or {}).get("nodes") or []
                    item_text = cls.extract_rich_text_nodes_text(nodes)
                    if item_text:
                        lines.append(f"- {item_text}")
            return "\n".join(lines).strip()

        if para_type == 6 or para.get("link_card"):
            return cls.extract_link_card_text(para.get("link_card"))

        if para_type == 7 or para.get("code"):
            code_obj = para.get("code") or {}
            if isinstance(code_obj, dict):
                return cls._first_str(
                    code_obj.get("content"),
                    code_obj.get("text"),
                    code_obj.get("raw"),
                )

        return ""

    @classmethod
    def extract_opus_paragraph_text(cls, opus: dict) -> str:
        """
        从 opus.content.paragraphs 里提取正文，并保留引用/列表/链接卡等段落。
        """
        if not isinstance(opus, dict):
            return ""

        content = opus.get("content") or {}
        paragraphs = content.get("paragraphs") or []

        if not isinstance(paragraphs, list):
            return ""

        parts: list[str] = []

        for para in paragraphs:
            text = cls.extract_paragraph_text(para)
            if text:
                parts.append(text)

        return "\n\n".join(parts).strip()

    @classmethod
    def extract_modules_list_text(cls, modules: Any) -> str:
        parts: list[str] = []

        for module in cls._iter_modules(modules):
            module_type = module.get("module_type")

            if module_type == "MODULE_TYPE_TOPIC":
                topic = module.get("module_topic") or {}
                if isinstance(topic, dict):
                    topic_text = cls._first_str(
                        topic.get("name"),
                        topic.get("title"),
                        topic.get("text"),
                    )
                    if topic_text:
                        topic_text = (
                            topic_text if topic_text.startswith("#") else f"#{topic_text}#"
                        )
                        parts.append(topic_text)

            if module_type == "MODULE_TYPE_CONTENT":
                content = module.get("module_content") or {}
                paragraphs = []
                if isinstance(content, dict):
                    paragraphs = content.get("paragraphs") or []
                if isinstance(paragraphs, list):
                    for para in paragraphs:
                        text = cls.extract_paragraph_text(para)
                        if text:
                            parts.append(text)

        return "\n\n".join(parts).strip()

    @classmethod
    def extract_dynamic_title(cls, item: dict, modules: Any) -> str | None:
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

        title_module = cls._module_by_type(modules, "MODULE_TYPE_TITLE")
        if title_module:
            module_title = title_module.get("module_title") or {}
            if isinstance(module_title, dict):
                title = cls._first_str(module_title.get("text"), module_title.get("title"))
                if title:
                    return title

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
    def extract_dynamic_text(cls, item: dict, modules: Any) -> str:
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
        8. opus modules 列表里的话题/段落/链接卡
        """
        modules_list_text = cls.extract_modules_list_text(modules)

        if not isinstance(modules, dict):
            return modules_list_text or "（无正文）"

        md = modules.get("module_dynamic") or {}
        if not isinstance(md, dict):
            return modules_list_text or "（无正文）"

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

        item_modules_text = ""
        if isinstance(item, dict):
            item_modules_text = cls.extract_modules_list_text(item.get("modules"))

        return modules_list_text or item_modules_text or "（无正文）"

    @classmethod
    def extract_modules_list_image_urls(cls, modules: Any) -> list[str]:
        image_urls: list[str] = []

        for module in cls._iter_modules(modules):
            module_type = module.get("module_type")

            if module_type == "MODULE_TYPE_TOP":
                display = ((module.get("module_top") or {}).get("display") or {})
                album = display.get("album") or {}
                pics = album.get("pics") or []
                if isinstance(pics, list):
                    for pic in pics:
                        if isinstance(pic, dict):
                            url = pic.get("url")
                            if isinstance(url, str) and url:
                                image_urls.append(url)

            if module_type == "MODULE_TYPE_CONTENT":
                content = module.get("module_content") or {}
                paragraphs = []
                if isinstance(content, dict):
                    paragraphs = content.get("paragraphs") or []
                if isinstance(paragraphs, list):
                    for para in paragraphs:
                        if not isinstance(para, dict):
                            continue
                        pic_obj = para.get("pic") or {}
                        pics = pic_obj.get("pics") or []
                        if isinstance(pics, list):
                            for pic in pics:
                                if isinstance(pic, dict):
                                    url = pic.get("url")
                                    if isinstance(url, str) and url:
                                        image_urls.append(url)

        return image_urls

    @classmethod
    def extract_dynamic_image_urls(cls, item: dict, modules: Any) -> list[str]:
        image_urls: list[str] = []

        image_urls.extend(cls.extract_modules_list_image_urls(modules))

        if not isinstance(modules, dict):
            return cls._dedupe_image_urls(image_urls)

        md = modules.get("module_dynamic") or {}
        if not isinstance(md, dict):
            return cls._dedupe_image_urls(image_urls)

        major = md.get("major") or {}
        if not isinstance(major, dict):
            return cls._dedupe_image_urls(image_urls)

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

        if isinstance(item, dict):
            image_urls.extend(cls.extract_modules_list_image_urls(item.get("modules")))

        return cls._dedupe_image_urls(image_urls)

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
        headers = self.parser.headers.copy()
        headers["Cache-Control"] = "no-cache"
        return await cached_image_to_data_uri(
            self._img_data_uri_cache,
            self.parser.http_get,
            img_url,
            headers=headers,
            referer=referer,
            max_bytes=max_bytes,
            timeout=10,
            debug_label="[Bilibili] dynamic image",
        )

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

        author_name, author_avatar, pub_ts = self.extract_author_info(modules)
        time_text = self.fmt_time(pub_ts)

        dynamic_title = self.extract_dynamic_title(item, modules)
        full_text = self.neutralize_at_text(self.extract_dynamic_text(item, modules))
        image_urls = self.extract_dynamic_image_urls(item, modules)

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
                f"dyn_service_v4"
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
