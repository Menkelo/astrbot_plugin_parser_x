import asyncio
import json
import time
from html import unescape
from typing import Any

from astrbot.api import logger
from bilibili_api.dynamic import Dynamic
from bilibili_api.exceptions import ResponseCodeException

from ...data import MediaContent
from ...utils import cached_image_to_data_uri, normalize_image_url
from ..base import ParseException


class BiliDynamicService:
    def __init__(self, parser):
        self.parser = parser
        self._img_data_uri_cache: dict[str, str | None] = {}

    @staticmethod
    def _build_delivery_contents(
        full_images: list[MediaContent],
    ) -> tuple[list[MediaContent], bool]:
        """Defer shared-card rendering to the sender and flag single-image works."""
        return list(full_images), len(full_images) == 1

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
    def _extract_assignment_json(html: str, marker: str) -> str | None:
        idx = html.find(marker)
        if idx == -1:
            return None

        eq = html.find("=", idx + len(marker))
        if eq == -1:
            return None

        start = eq + 1
        while start < len(html) and html[start].isspace():
            start += 1

        if start >= len(html) or html[start] not in "{[":
            return None

        stack: list[str] = []
        in_string = False
        quote = ""
        escaped = False
        pairs = {"{": "}", "[": "]"}

        for pos in range(start, len(html)):
            ch = html[pos]

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    in_string = False
                continue

            if ch in {"'", '"'}:
                in_string = True
                quote = ch
                continue

            if ch in pairs:
                stack.append(pairs[ch])
                continue

            if stack and ch == stack[-1]:
                stack.pop()
                if not stack:
                    return html[start : pos + 1]

        return None

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

    @staticmethod
    def cap_bili_image_url(url: str, *, width: int = 1280, quality: int = 85) -> str:
        """
        给 B站图片 URL 追加 CDN 尺寸/质量参数，显著降低单图体积。

        B站 hdslb CDN 支持 `@<宽>w_<质量>q.<格式>` 语法，例如
        `.../abc.jpg@1280w_85q.jpg`，服务端会直接返回缩放压缩后的图。
        动态原图常达数 MB，10 张一起走合并转发极易触发 NapCat 上传超时；
        限到 1280 宽后单图通常只剩几百 KB，既能成功合并转发又更快。

        - 仅处理 hdslb / bilivideo 图片域名；
        - GIF/WebP 保持原地址，避免动画被 CDN 转码为静态 JPEG；
        - 已带 `@` 参数的 URL 不重复追加；
        - 非图片或异常一律原样返回，保证安全。
        """
        if not isinstance(url, str) or not url:
            return url

        low = url.lower()
        if "@" in url:
            return url
        if not any(d in low for d in ("hdslb.com", "bilivideo.com", "biliimg.com")):
            return url

        # @ 尺寸参数必须接在路径末尾、查询串之前
        path_part, sep, query = url.partition("?")
        if path_part.lower().endswith((".gif", ".webp")):
            return url
        if not any(
            path_part.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png")
        ):
            return url

        return f"{path_part}@{width}w_{quality}q.jpg{sep}{query}"

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
    def _pic_to_image_url(cls, pic: Any) -> str | None:
        if not isinstance(pic, dict):
            return None

        image_url = cls._first_str(pic.get("url"), pic.get("src"))
        return image_url or None

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
                    nodes = (
                        item.get("nodes") or (item.get("text") or {}).get("nodes") or []
                    )
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
                            topic_text
                            if topic_text.startswith("#")
                            else f"#{topic_text}#"
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
                title = cls._first_str(
                    module_title.get("text"), module_title.get("title")
                )
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
    def _extract_dynamic_text_value(cls, item: dict, modules: Any) -> str:
        modules_list_text = cls.extract_modules_list_text(modules)

        if not isinstance(modules, dict):
            return modules_list_text

        md = modules.get("module_dynamic") or {}
        if not isinstance(md, dict):
            return modules_list_text

        desc = md.get("desc") or {}
        if isinstance(desc, dict):
            text = desc.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

            nodes_text = cls.extract_rich_text_nodes_text(
                desc.get("rich_text_nodes") or []
            )
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

        return modules_list_text or item_modules_text

    @classmethod
    def extract_dynamic_text(cls, item: dict, modules: Any) -> str:
        """
        稳定提取 B站动态正文，并在转发动态中补回原动态正文。

        优先级：
        1. module_dynamic.desc.text / rich_text_nodes
        2. module_dynamic.major.opus.summary / content.paragraphs
        3. module_dynamic.major.archive.desc / title
        4. opus modules 列表里的话题、段落和链接卡
        5. item.orig 中的原动态正文
        """
        primary_text = cls._extract_dynamic_text_value(item, modules)
        original = item.get("orig") if isinstance(item, dict) else None
        if not isinstance(original, dict):
            return primary_text or "（无正文）"

        original_modules = original.get("modules") or {}
        original_text = cls._extract_dynamic_text_value(original, original_modules)
        if not original_text:
            return primary_text or "（无正文）"

        original_author, _, _ = cls.extract_author_info(original_modules)
        original_label = (
            f"转发自 @{original_author}："
            if original_author and original_author != "B站用户"
            else "转发动态："
        )
        original_body = f"{original_label}\n{original_text}"
        if primary_text and primary_text not in {"转发动态", "转发"}:
            if primary_text != original_text:
                return f"{primary_text}\n\n{original_body}"
        return original_body

    @classmethod
    def extract_modules_list_image_urls(cls, modules: Any) -> list[str]:
        image_urls: list[str] = []

        for module in cls._iter_modules(modules):
            module_type = module.get("module_type")

            if module_type == "MODULE_TYPE_TOP":
                display = (module.get("module_top") or {}).get("display") or {}
                album = display.get("album") or {}
                pics = album.get("pics") or []
                if isinstance(pics, list):
                    for pic in pics:
                        image_url = cls._pic_to_image_url(pic)
                        if image_url:
                            image_urls.append(image_url)

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
                                image_url = cls._pic_to_image_url(pic)
                                if image_url:
                                    image_urls.append(image_url)

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
                    image_url = cls._pic_to_image_url(p)
                    if image_url:
                        image_urls.append(image_url)

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
                            image_url = cls._pic_to_image_url(p)
                            if image_url:
                                image_urls.append(image_url)

        archive = major.get("archive") or {}
        if isinstance(archive, dict):
            cover = archive.get("cover")
            if isinstance(cover, str) and cover:
                image_urls.append(cover)

        if isinstance(item, dict):
            image_urls.extend(cls.extract_modules_list_image_urls(item.get("modules")))

            original = item.get("orig")
            if isinstance(original, dict):
                image_urls.extend(
                    cls.extract_dynamic_image_urls(
                        original,
                        original.get("modules") or {},
                    )
                )

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
        将 B站图片转 data URI，避免远端 Canvas 渲染时头像加载失败。
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

    async def _fetch_opus_page_raw(self, dynamic_id: int) -> dict | None:
        url = f"https://www.bilibili.com/opus/{dynamic_id}"
        headers = self.parser.headers.copy()
        headers["Referer"] = "https://www.bilibili.com/"

        if self.parser.bili_ck:
            headers["Cookie"] = self.parser.bili_ck

        try:
            resp = await self.parser.http_get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=12,
            )
        except Exception as e:
            logger.debug(f"[Bilibili] opus页面兜底请求失败: {e}")
            return None

        html = resp.text or ""
        json_text = self._extract_assignment_json(html, "window.__INITIAL_STATE__")
        if not json_text:
            return None

        try:
            data = json.loads(json_text)
        except Exception as e:
            logger.debug(f"[Bilibili] opus页面INITIAL_STATE解析失败: {e}")
            return None

        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict) and detail.get("modules"):
            return {"item": detail}

        return None

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
            page_raw = await self._fetch_opus_page_raw(dynamic_id)
            if page_raw:
                return page_raw

            raise ParseException(
                f"B站动态解析失败（疑似风控 -352），建议配置 bili_ck 后重试: {e or last_err}"
            ) from (e if isinstance(e, Exception) else last_err)

    async def parse_dynamic(self, dynamic_id: int):
        raw_dynamic = await self._fetch_dynamic_raw(dynamic_id)

        item = (raw_dynamic or {}).get("item") or {}
        modules = item.get("modules") or {}

        author_name, author_avatar, pub_ts = self.extract_author_info(modules)

        dynamic_title = self.extract_dynamic_title(item, modules)
        original = item.get("orig") if isinstance(item, dict) else None
        if not dynamic_title and isinstance(original, dict):
            dynamic_title = self.extract_dynamic_title(
                original,
                original.get("modules") or {},
            )

        full_text = self.extract_dynamic_text(item, modules)
        image_urls = self.extract_dynamic_image_urls(item, modules)

        if not full_text or full_text == "（无正文）":
            page_raw = await self._fetch_opus_page_raw(dynamic_id)
            page_item = (page_raw or {}).get("item") or {}
            page_modules = page_item.get("modules") or {}
            page_text = self.extract_dynamic_text(page_item, page_modules)
            if page_text and page_text != "（无正文）":
                full_text = page_text
            if not dynamic_title:
                dynamic_title = self.extract_dynamic_title(page_item, page_modules)
            image_urls = self._dedupe_image_urls(
                [
                    *image_urls,
                    *self.extract_dynamic_image_urls(page_item, page_modules),
                ]
            )

        full_text = self.neutralize_at_text(full_text or "（无正文）")

        media_headers = self.parser.headers.copy()
        media_headers["Referer"] = f"https://www.bilibili.com/opus/{dynamic_id}"
        # 限制单图尺寸/体积，避免 10+ 张高清原图合并转发时撑爆 NapCat 上传超时
        capped_image_urls = [self.cap_bili_image_url(u) for u in image_urls]
        full_images: list[MediaContent] = (
            self.parser.create_image_contents(
                capped_image_urls, ext_headers=media_headers
            )
            if capped_image_urls
            else []
        )

        contents, single_image_work = self._build_delivery_contents(full_images)

        author_avatar_data_uri = None
        if not single_image_work:
            author_avatar_data_uri = await self.img_to_data_uri(
                author_avatar,
                referer=f"https://t.bilibili.com/{dynamic_id}",
            )

        logger.info(
            f"[Bilibili][诊断] 动态附加图片 {len(full_images)} 张; "
            f"正文 {len(full_text or '')} 字; 单图直发={single_image_work}"
        )

        extra = {
            "reply_original_for_single_image": single_image_work,
            "send_text": single_image_work,
        }
        if not single_image_work:
            card_info = ["正文完整保留"]
            if full_images:
                card_info.append(f"媒体 {len(full_images)} 项")
            extra.update(
                {
                    "render_text_card": True,
                    "text_card_avatar": author_avatar_data_uri or author_avatar or "",
                    "text_card_media": image_urls[0] if image_urls else "",
                    "card_platform_name": "B站动态",
                    "card_kind": "动态 · 图文" if full_images else "动态",
                    "card_author_badge": "UP主",
                    "card_info": card_info,
                }
            )

        return self.parser.result(
            title=dynamic_title,
            contents=contents,
            text=full_text,
            author=self.parser.create_author(author_name, author_avatar),
            timestamp=pub_ts,
            url=f"https://t.bilibili.com/{dynamic_id}",
            # 单图作品直接引用触发解析的原消息；多图和纯文字动态由
            # 插件发送阶段渲染统一长卡，避免解析阶段阻塞 Canvas。
            extra=extra,
        )
