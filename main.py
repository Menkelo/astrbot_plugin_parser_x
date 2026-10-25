import asyncio
import hashlib
import json
import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Json
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.card_service import CardServiceMixin
from .core.comment_canvas import CommentDocument
from .core.card_guard import ignored_component_reason, is_card_segment
from .core.comment_settings import parse_bool
from .core.config_policy import migrate_config
from .core.data import ImageContent, ParseResult
from .core.debug_api import PLUGIN_NAME, DebugApiMixin
from .core.delivery import DeliveryMixin, _GeneratedCardContent, _prepare_media_file
from .core.debug_page import DebugMediaRegistry, DebugSessionManager
from .core.download import Downloader
from .core.exception import ParseException, SizeLimitException, SkipParseException
from .core.html_renderer import HtmlRenderService
from .core.live_guard import (
    is_live_share_caption,
    is_live_url,
    is_short_link,
    looks_like_live_card,
    looks_like_live_share,
)
from .core.nonwork_guard import is_nonwork_url
from .core.parsers import BaseParser
from .core.parsers.bilibili.comment_canvas import BiliCommentDocument
from .core.result_cache import ParseResultCache, _config_signature
from .core.async_cache import AsyncSingleFlight
from .core.unified_renderer import UnifiedCardRenderer
from .core.utils import extract_json_url, pick_supported_share_url


class ParserXPlugin(CardServiceMixin, DeliveryMixin, DebugApiMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        config["data_dir"] = str(self.data_dir)
        self.cache_dir: Path = self.data_dir / "cache_dir"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config["cache_dir"] = str(self.cache_dir)

        migrate_config(config)
        self.config.save_config()

        self.parser_map: dict[str, BaseParser] = {}
        self.result_cache = ParseResultCache()
        self.comment_document_cache = AsyncSingleFlight()
        self.comment_render_cache = AsyncSingleFlight()
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        self.downloader = Downloader(config)
        self.arbiter = EmojiLikeArbiter()
        self.cleaner = CacheCleaner(self.context, self.config)
        self.render_service = HtmlRenderService.from_config(config)
        self.text_renderer = UnifiedCardRenderer(self.render_service)
        self.debug_media = DebugMediaRegistry()
        self.debug_sessions = DebugSessionManager()
        self._register_debug_page_apis()

    # region 生命周期

    async def initialize(self):
        self._register_parser()

    async def terminate(self):
        await self.debug_sessions.close()
        for name in ("comment_render_cache", "comment_document_cache", "result_cache"):
            if cache := getattr(self, name, None):
                await cache.close()
        await self.downloader.close()
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        await self.cleaner.stop()
        await self.render_service.aclose()

    def _register_parser(self):
        all_subclass = BaseParser.get_all_subclass()
        platform_names = []
        for _cls in all_subclass:
            platform_name = _cls.platform.name
            if not self._platform_enabled(platform_name):
                logger.info(f"Parser X 已禁用平台: {_cls.platform.display_name}")
                continue
            parser = _cls(self.config, self.downloader)
            if hasattr(parser, "set_render_service"):
                parser.set_render_service(self.render_service)
            platform_names.append(parser.platform.display_name)
            for keyword, _ in _cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.info(f"Parser X 已加载平台: {'、'.join(platform_names) or '无'}")

        patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(pt) if isinstance(pt, str) else pt)
            for cls in all_subclass
            if self._platform_enabled(cls.platform.name)
            for kw, pt in cls._key_patterns
        ]
        patterns.sort(key=lambda x: -len(x[0]))
        self.key_pattern_list = patterns

    def _platform_enabled(self, platform_name: str) -> bool:
        return True

    def _behavior(self) -> dict:
        behavior = self.config.get("behavior", {})
        return behavior if isinstance(behavior, dict) else {}

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        origin = event.unified_msg_origin
        parts = origin.rsplit(":", 2)
        if len(parts) == 3 and parts[1] == "GroupMessage" and parts[2].isdigit():
            return parts[2]
        return origin

    def _disabled_sessions(self) -> list:
        sessions = self._behavior().get("disabled_sessions", [])
        if isinstance(sessions, list):
            return sessions
        legacy = self.config.get("disabled_sessions", [])
        return legacy if isinstance(legacy, list) else []

    def _save_disabled_sessions(self, sessions: list) -> None:
        behavior = self._behavior()
        behavior["disabled_sessions"] = sessions
        self.config["behavior"] = behavior
        self.config.save_config()

    def _show_download_fail_tip(self) -> bool:
        return True

    def _debug_mode_enabled(self) -> bool:
        config = getattr(self, "config", {})
        debug = config.get("debug", {}) if hasattr(config, "get") else {}
        if not isinstance(debug, dict):
            return False
        return parse_bool(debug.get("enabled", False), False)

    # endregion

    # region 正文卡准备与投递结果持久化

    async def _send_parse_result(self, event: AstrMessageEvent, result: ParseResult):
        await self._deliver_parse_result(event, result)
        key = result.extra.get("_disk_result_key")
        if key and result.extra.get("cache_result") is not False:
            comments = getattr(self, "config", {}).get("comments", {})
            cards_enabled = not isinstance(comments, dict) or parse_bool(comments.get("enabled", True), True)
            if (not cards_enabled or not result.extra.get("render_text_card")
                    or result.extra.get("_disk_card_path")):
                from .core.disk_result_cache import DiskResultCache
                result.extra["_disk_has_comments"] = bool(
                    result.extra.get("_disk_has_comments")
                    or callable(result.extra.get("text_card_comment_factory"))
                )
                try:
                    await DiskResultCache(self.cache_dir).save(key, result)
                except Exception as exc:
                    logger.warning(f"解析结果缓存保存失败: {type(exc).__name__}")


    # endregion

    @staticmethod
    def _looks_like_live_share(text: str) -> bool:
        return looks_like_live_share(text)

    # Short links hide their target until the redirect resolves; their caption is
    # the only pre-flight signal that the share is a live room.
    LIVE_SHORT_LINK_KEYWORDS = frozenset(
        {
            "v.douyin",
            "jx.douyin",
            "b23.tv",
            "bili2233",
            "xhslink.com",
            "xhslink.cn",
            "v.kuaishou",
            "chenzhongtech",
        }
    )

    def _should_ignore_live_share(
        self,
        keyword: str,
        raw_match: str,
        text: str,
    ) -> bool:
        if keyword in self.LIVE_SHORT_LINK_KEYWORDS and is_live_share_caption(text):
            return True

        low_url = (raw_match or "").lower()
        low_text = (text or "").lower()

        # Every platform: a live room URL is never answered and never parsed.
        # 有的平台正则只截到 "kuaishou.com/u/..." 而丢掉 live./webcast. 前缀，
        # 所以再回原文里按完整写法找一次。
        if is_live_url(raw_match) or (low_url and f"live.{low_url}" in low_text):
            return True

        if keyword in {"v.kuaishou", "kuaishou", "chenzhongtech"}:
            return (
                "live.kuaishou.com" in low_url
                or "/live/" in low_url
                or is_live_share_caption(text)
            )

        if keyword in {"xhslink.com", "xhslink.cn"}:
            return "livestream" in low_text or (
                "小红书" in text and self._looks_like_live_share(text)
            )

        if keyword in {"v.douyin", "jx.douyin"}:
            # 主页分享（"查看TA的更多作品"）不是作品，入口直接丢弃，
            # 免得解析器跳转后拿不到 aweme_id 再报一堆错误。
            if any(marker in text for marker in ("更多作品", "的主页")):
                return True
            return is_live_share_caption(text)

        return False

    @staticmethod
    def _match_has_hostname_boundary(text: str, searched: re.Match[str]) -> bool:
        """Validate the enclosing authority, not just a regex's matched substring.

        Most decorators omit the scheme (some omit www); XHS note decorators
        match only the relative route. Keep those match objects unchanged for
        dispatch while refusing domains embedded in unrelated hosts or paths.
        Bare BV/av identifiers have no enclosing URL and remain supported.
        """
        for url in re.finditer(
            r"(?:https?://)?(?P<host>(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})"
            r"(?::\d+)?/[^\s<>\"'，。；！？）]*",
            text,
            re.I,
        ):
            if not (url.start() <= searched.start() < url.end()):
                continue
            host = url.group("host").lower()
            if searched.start() < url.end("host"):
                candidate = re.match(
                    r"(?:https?://)?((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?::\d+)?/",
                    searched.group(0),
                    re.I,
                )
                if candidate is None:
                    return False
                matched_host = candidate.group(1).lower()
                return host == matched_host or host.endswith("." + matched_host)
            # XHS decorators deliberately return a relative route, not a URL.
            return (
                (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
                and searched.start() == url.end("host") + 1
                and searched.group(0).startswith(("explore/", "discovery/item/"))
            )
        return True

    def _collect_parser_matches(
        self,
        text: str,
    ) -> list[tuple[BaseParser, str, re.Match[str]]]:
        matches: list[tuple[int, str, re.Match[str]]] = []
        for keyword, pattern in self.key_pattern_list:
            if keyword not in text:
                continue
            for searched in pattern.finditer(text):
                if not self._match_has_hostname_boundary(text, searched):
                    continue
                if is_nonwork_url(searched.group(0)):
                    continue
                if self._should_ignore_live_share(
                    keyword,
                    searched.group(0),
                    text,
                ):
                    logger.info(
                        f"跳过直播链接（{searched.group(0).strip()}）：不响应、不解析"
                    )
                    continue
                matches.append((searched.start(), keyword, searched))

        matches.sort(key=lambda item: (item[0], -len(item[1])))
        processed_matches: set[tuple[str, str]] = set()
        accepted_spans: list[tuple[int, int]] = []
        accepted: list[tuple[BaseParser, str, re.Match[str]]] = []

        for _, keyword, searched in matches:
            parser = self.parser_map.get(keyword)
            if parser is None:
                continue

            span = (searched.start(), searched.end())
            if any(
                span[0] < accepted_end and accepted_start < span[1]
                for accepted_start, accepted_end in accepted_spans
            ):
                continue

            raw_match = searched.group(0).strip()
            match_url = re.sub(
                r"^https?://",
                "",
                raw_match,
                flags=re.IGNORECASE,
            )
            match_url = match_url.rstrip(").,;!?，。；！？）]")
            match_key = (parser.platform.name, match_url.casefold())
            if match_key in processed_matches:
                continue

            processed_matches.add(match_key)
            accepted_spans.append(span)
            accepted.append((parser, keyword, searched))

        return self._prefer_xiaohongshu_note_matches(accepted)

    @staticmethod
    def _xiaohongshu_note_id(raw_match: str) -> str:
        matched = re.search(
            r"(?:explore|discovery/item)/([0-9a-zA-Z]+)",
            raw_match,
            re.I,
        )
        return matched.group(1).lower() if matched else ""

    @classmethod
    def _prefer_xiaohongshu_note_matches(
        cls,
        accepted: list[tuple[BaseParser, str, re.Match[str]]],
    ) -> list[tuple[BaseParser, str, re.Match[str]]]:
        # An opaque short link is not proof of identity with any full note in
        # the message. Only deduplicate routes carrying the same known note ID.
        preferred: dict[str, tuple[int, tuple[BaseParser, str, re.Match[str]]]] = {}
        for item in accepted:
            parser, keyword, searched = item
            if (
                parser.platform.name != "xiaohongshu"
                or keyword in {"xhslink.com", "xhslink.cn"}
            ):
                continue
            note_id = cls._xiaohongshu_note_id(searched.group(0))
            if not note_id:
                continue
            score = 1 if "xsec_token=" in searched.group(0).lower() else 0
            existing = preferred.get(note_id)
            if existing is None or score > existing[0]:
                preferred[note_id] = (score, item)

        if not preferred:
            return accepted

        merged: list[tuple[BaseParser, str, re.Match[str]]] = []
        used_ids: set[str] = set()
        for item in accepted:
            parser, keyword, searched = item
            if (
                parser.platform.name != "xiaohongshu"
                or keyword in {"xhslink.com", "xhslink.cn"}
            ):
                merged.append(item)
                continue
            note_id = cls._xiaohongshu_note_id(searched.group(0))
            if not note_id:
                merged.append(item)
                continue
            if note_id in used_ids:
                continue
            used_ids.add(note_id)
            merged.append(preferred[note_id][1])
        return merged

    @staticmethod
    def _is_share_card_segment(segment: object) -> bool:
        if isinstance(segment, Json):
            return True
        return type(segment).__name__ in {"Json", "Xml"}

    def _text_from_event(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        chain = event.get_messages()
        if not chain:
            return text

        card_urls: list[str] = []
        for segment in chain:
            if not self._is_share_card_segment(segment):
                continue
            try:
                url = extract_json_url(getattr(segment, "data", None)) or ""
            except Exception:
                url = ""
            if url:
                card_urls.append(url)

        if not card_urls:
            return text

        best = pick_supported_share_url(card_urls)
        if not best:
            return text
        if not text:
            return best
        if best in text:
            return text
        return f"{text}\n{best}"

    def _live_card_reason(self, chain: list) -> str:
        """分享卡片本身是直播时给出原因；纯文本消息不在此判定。

        卡片里带着平台自己的标题（如「⚡直播中⚡某某的直播间」），
        这类消息连跳转都不该发生，直接整条丢弃。
        """
        for segment in chain or ():
            if not self._is_share_card_segment(segment):
                continue
            data = getattr(segment, "data", None)
            try:
                url = extract_json_url(data) or ""
            except Exception:
                url = ""
            if url and is_live_url(url):
                return "直播卡片链接"
            raw = data if isinstance(data, str) else json.dumps(
                data, ensure_ascii=False, default=str
            )
            if not looks_like_live_card(raw):
                continue
            # 卡片文案像直播，且卡片给的地址要么是短链（跳转前看不出目标），
            # 要么根本没提取到地址；直接指向普通视频的卡片不受影响。
            if not url or is_short_link(url):
                return "直播分享卡片"
        return ""

    async def _parse_with_cache(
        self, parser: BaseParser, keyword: str, searched: re.Match[str], text: str
    ) -> ParseResult:
        cache = getattr(self, "result_cache", None)
        if cache is None:
            cache = self.result_cache = ParseResultCache()
        config = getattr(self, "config", {})
        disk_key = None
        if hasattr(self, "cache_dir"):
            from .core.disk_result_cache import DiskResultCache
            identity = ["disk_result_v2", _config_signature(config),
                        type(parser).__module__, type(parser).__qualname__,
                        keyword, searched.group(0), searched.groups(), text.strip()]
            disk_key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, default=str).encode()).hexdigest()
            restored = DiskResultCache(self.cache_dir).load(disk_key)
            if restored is not None:
                return restored
        result = await cache.parse(parser, keyword, searched, text, config)
        if disk_key:
            result.extra["_disk_result_key"] = disk_key
        return result


    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听 OneBot 消息并解析其中的支持平台分享链接。"""
        if self._debug_mode_enabled():
            return
        if not isinstance(event, AiocqhttpMessageEvent):
            return
        umo = event.unified_msg_origin
        text = self._text_from_event(event)

        if self._session_key(event) in self._disabled_sessions():
            return

        chain = event.get_messages() or []
        ignored_reason = ignored_component_reason(chain)
        if ignored_reason:
            logger.info(f"忽略非常规卡片消息（{ignored_reason}）：不响应、不解析")
            return

        live_reason = self._live_card_reason(chain)
        if live_reason:
            logger.info(f"跳过直播分享（{live_reason}）：不响应、不解析")
            return

        if not text:
            if any(is_card_segment(segment) for segment in chain):
                logger.info("忽略非常规卡片消息（分享卡片中没有可解析链接）：不响应、不解析")
            return

        prefixes = self.context.get_config().get("command_prefixes", ["/"])
        is_command = any(text.startswith(p) for p in prefixes)
        if is_command:
            return

        collected_matches = self._collect_parser_matches(text)
        if not collected_matches:
            return

        notified = False
        for parser, keyword, searched in collected_matches:
            try:
                parse_res = await self._parse_with_cache(
                    parser, keyword, searched, text
                )
                # 只在此刻贴表情：解析真的产出了结果。
                # 直播间/被跳过的链接会走 SkipParseException，永远贴不到表情。
                if (self._behavior().get("reaction_enabled", True)
                        and not notified and isinstance(event, AiocqhttpMessageEvent)):
                    if hasattr(event.message_obj, "message_id"):
                        notified = True
                        asyncio.create_task(
                            self.arbiter.notify(event.bot, event.message_obj.message_id)
                        )
                await self._send_parse_result(event, parse_res)
            except SkipParseException:
                continue
            except SizeLimitException as e:
                await event.send(event.plain_result(f"⚠️ {e}"))
            except ParseException as e:
                await event.send(event.plain_result(f"⚠️ {e}"))
            except Exception:
                logger.exception("解析过程中发生未知错误")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = self._session_key(event)
        sessions = self._disabled_sessions()
        if umo in sessions:
            sessions.remove(umo)
            self._save_disabled_sessions(sessions)
            yield event.plain_result("解析已开启")
        else:
            yield event.plain_result("解析已开启，无需重复开启")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = self._session_key(event)
        sessions = self._disabled_sessions()
        if umo not in sessions:
            sessions.append(umo)
            self._save_disabled_sessions(sessions)
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")

