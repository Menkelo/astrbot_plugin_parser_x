import asyncio
import re
import time
from pathlib import Path
from re import Match
from typing import ClassVar

from bilibili_api import Credential, request_settings, select_client
from bilibili_api.video import Video
from msgspec import convert

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ...constants import BILIBILI_HEADER
from ...data import Platform
from ...exception import SizeLimitException
from ...utils import ck2dict
from ..base import BaseParser, Downloader, ParseException, handle

from .comment_renderer import BiliCommentRenderer
from .comment_service import BiliCommentService
from .dynamic_renderer import BiliDynamicRenderer
from .dynamic_service import BiliDynamicService
from .live_service import BiliLiveService
from .stream_selector import BiliStreamSelector
from ...live_renderer import LiveCardRenderer


class BilibiliParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="bilibili", display_name="B站")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

        select_client("curl_cffi")
        request_settings.set("impersonate", "chrome120")
        request_settings.set("verify", False)

        self.headers = BILIBILI_HEADER.copy()
        self._credential: Credential | None = None
        self._credential_inited = False
        self.cache_dir = Path(config["cache_dir"])
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.bili_ck = config.get("cookies", {}).get("bili_ck", "")
        self.comment_limit = 9

        perf = config.get("performance", {})
        if not isinstance(perf, dict):
            perf = {}

        self.max_size_mb = perf.get("source_max_size", 90)

        # === B站视频编码偏好 ===
        # auto：自动选择，默认 HEVC > AV1 > AVC
        # hevc：优先 HEVC/H.265
        # av1 ：优先 AV1
        # avc ：优先 AVC/H.264
        video_codec_conf = perf.get("video_codec", "hevc")

        if isinstance(video_codec_conf, dict):
            video_codec_conf = video_codec_conf.get("value") or video_codec_conf.get("label") or "hevc"

        self.video_codec = str(video_codec_conf).lower()

        # 已移除 AV1，只保留 auto / hevc / avc
        if self.video_codec not in {"auto", "hevc", "avc"}:
            self.video_codec = "hevc"

        logger.info(f"[Bilibili] 视频编码偏好: {self.video_codec}")

        # === B站评论区总开关 ===
        # true：解析B站视频时抓取并渲染评论区
        # false：只解析视频，不抓取评论区
        self.enable_comment_card = bool(config.get("bili_comment", True))

        self._cache_ttl = int(perf.get("bili_cache_ttl", 120))
        self._cache_max = int(perf.get("bili_cache_max", 256))
        self._video_info_cache: dict[str, tuple[float, dict]] = {}
        self._playurl_cache: dict[str, tuple[float, dict]] = {}

        comment_conf = config.get("comment_filter", {})
        if not isinstance(comment_conf, dict):
            comment_conf = {}

        self.comment_renderer = BiliCommentRenderer()
        self.comment_service = BiliCommentService(
            parser=self,
            renderer=self.comment_renderer,
            comment_limit=self.comment_limit,
            enable_text_ad_filter=bool(comment_conf.get("enable_text_ad_filter", True)),
            enable_qr_filter=bool(comment_conf.get("enable_qr_filter", True)),
            qr_check_max=int(comment_conf.get("qr_check_max", 4)),
            qr_check_timeout=float(comment_conf.get("qr_check_timeout", 6)),
        )

        self.stream_selector = BiliStreamSelector()

        self.live_renderer = LiveCardRenderer()
        self.dynamic_renderer = BiliDynamicRenderer()

        self.live_service = BiliLiveService(self)
        self.dynamic_service = BiliDynamicService(self)

    # region 路由

    @handle("b23.tv", r"(?:https?://)?b23\.tv/[A-Za-z\d\._?%&+\-=/#]+")
    @handle("bili2233", r"(?:https?://)?bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+")
    async def _parse_short_link(self, searched: Match[str]):
        raw = searched.group(0)
        url = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"

        final_url = await self._resolve_short_link_by_head(url)
        if final_url:
            try:
                keyword, matched = self.search_url(final_url)
                if keyword not in {"b23.tv", "bili2233"}:
                    return await self.parse(keyword, matched)
            except ParseException as e:
                logger.debug(f"[Bilibili] HEAD短链解析未命中，回退GET重定向: {e}")

        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/BV",
        r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/(?:video/)?(?P<bvid>BV[0-9a-zA-Z]{10})(?:[/?#][^\s]*)?",
    )
    async def _parse_bv(self, searched: Match[str]):
        bvid = str(searched.group("bvid"))
        page_num = int((searched.groupdict().get("page_num") or 1))

        if page_num == 1:
            m = re.search(r"[?&]p=(\d{1,3})", searched.group(0))
            if m:
                page_num = int(m.group(1))

        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/av",
        r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/(?:video/)?av(?P<avid>\d{6,})(?:[/?#][^\s]*)?",
    )
    async def _parse_av(self, searched: Match[str]):
        avid = int(searched.group("avid"))
        page_num = int((searched.groupdict().get("page_num") or 1))

        if page_num == 1:
            m = re.search(r"[?&]p=(\d{1,3})", searched.group(0))
            if m:
                page_num = int(m.group(1))

        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("t.bili", r"(?:https?://)?t\.bilibili\.com/(?P<dynamic_id>\d+)(?:[/?#][^\s]*)?")
    async def _parse_dynamic(self, searched: Match[str]):
        return await self.parse_dynamic(int(searched.group("dynamic_id")))

    @handle(
        "opus",
        r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/opus/(?P<dynamic_id>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_opus(self, searched: Match[str]):
        return await self.parse_dynamic(int(searched.group("dynamic_id")))

    @handle(
        "live.bilibili.com/",
        r"(?:https?://)?live\.bilibili\.com/(?P<room_id>\d+)(?:\?[^\s#]*)?(?:#[^\s]*)?",
    )
    async def _parse_live(self, searched: Match[str]):
        return await self.parse_live(int(searched.group("room_id")))

    # endregion

    # region 通用工具

    async def _resolve_short_link_by_head(self, url: str) -> str | None:
        """
        b23/bili2233 只需要最终落点；优先用 HEAD 跟随跳转，避免拉取页面正文。
        HEAD 被拒或未拿到可解析 URL 时，调用方会回退到原有 GET 重定向逻辑。
        """
        try:
            resp = await self.client.head(
                url,
                headers=self.headers,
                allow_redirects=True,
                timeout=8,
                verify=False,
            )
        except Exception as e:
            logger.debug(f"[Bilibili] HEAD短链解析失败: {url} | {e}")
            return None

        if getattr(resp, "status_code", 0) >= 400:
            return None

        final_url = str(getattr(resp, "url", "") or "").strip()
        if not final_url or final_url == url:
            return None

        return final_url

    @staticmethod
    def norm_bili_img(url: str | None) -> str | None:
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
    def norm_bili_ts(ts) -> int | None:
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
    def fmt_bili_time(ts: int | None) -> str | None:
        if not ts:
            return None

        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
        except Exception:
            return None

    # endregion

    # region 缓存辅助

    def _cache_get(self, cache: dict[str, tuple[float, dict]], key: str) -> dict | None:
        item = cache.get(key)
        if not item:
            return None

        ts, val = item
        if time.time() - ts > self._cache_ttl:
            cache.pop(key, None)
            return None

        return val

    def _cache_set(self, cache: dict[str, tuple[float, dict]], key: str, val: dict):
        cache[key] = (time.time(), val)
        # 容量上限保护：TTL 仅在访问时清理，长时间不命中的条目会持续堆积，
        # 这里按插入顺序淘汰最旧项，避免内存无上限增长。
        while len(cache) > self._cache_max:
            cache.pop(next(iter(cache)), None)

    async def _get_video_info_cached(self, video: Video, cache_key: str) -> dict:
        if cached := self._cache_get(self._video_info_cache, cache_key):
            return cached

        data = await video.get_info()
        self._cache_set(self._video_info_cache, cache_key, data)
        return data

    async def _get_playurl_cached(
        self,
        video: Video,
        page_index: int,
        cache_key: str,
        *,
        qn: int | None = None,
    ) -> dict:
        qkey = f"{cache_key}:qn={qn or 'default'}"
        if cached := self._cache_get(self._playurl_cache, qkey):
            return cached

        if qn is None:
            data = await video.get_download_url(page_index=page_index)
        else:
            try:
                data = await video.get_download_url(page_index=page_index, qn=qn)
            except TypeError:
                try:
                    data = await video.get_download_url(page_index=page_index, quality=qn)
                except TypeError:
                    data = await video.get_download_url(page_index=page_index)

        self._cache_set(self._playurl_cache, qkey, data)
        return data

    # endregion

    # region 流地址选择

    @staticmethod
    def _detect_codec_type(codecs: str | None) -> str:
        """
        识别 B站 DASH 视频流编码类型。

        常见 codecs：
        - av01 / av1             -> AV1
        - hev1 / hvc1 / hevc     -> HEVC/H.265
        - avc1 / avc / h264      -> AVC/H.264
        """
        c = (codecs or "").lower()

        if "av01" in c or "av1" in c:
            return "av1"

        if "hev1" in c or "hvc1" in c or "hevc" in c or "h265" in c:
            return "hevc"

        if "avc1" in c or "avc" in c or "h264" in c:
            return "avc"

        return "unknown"

    def _codec_rank(self, codecs: str | None) -> int:
        """
        根据 performance.video_codec 返回编码排序权重。
        数字越小优先级越高。

        已移除 AV1：
        - AV1 流会被跳过，不参与排序；
        - auto/hevc：HEVC > AVC；
        - avc：AVC > HEVC。
        """
        codec_type = self._detect_codec_type(codecs)

        if codec_type == "av1":
            return 999

        if self.video_codec == "avc":
            order = {
                "avc": 0,
                "hevc": 1,
                "unknown": 9,
            }
        else:
            # auto / hevc 默认策略：
            # HEVC/H.265 优先，其次 AVC/H.264
            order = {
                "hevc": 0,
                "avc": 1,
                "unknown": 9,
            }

        return order.get(codec_type, 9)

    @staticmethod
    def _collect_stream_urls(item: dict) -> list[str]:
        urls: list[str] = []

        if not isinstance(item, dict):
            return urls

        base = item.get("baseUrl") or item.get("base_url") or item.get("url")
        if isinstance(base, str) and base:
            urls.append(base)

        backups = item.get("backupUrl") or item.get("backup_url") or item.get("backup_urls") or []
        if isinstance(backups, list):
            for u in backups:
                if isinstance(u, str) and u:
                    urls.append(u)

        seen = set()
        uniq = []

        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)

        def score(u: str) -> tuple[int, int]:
            return (
                1 if ":8082" in u else 0,
                1 if "mcdn" in u else 0,
            )

        uniq.sort(key=score)
        return uniq

    @staticmethod
    def _unwrap_playurl_data(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}

        if "dash" in data or "durl" in data:
            return data

        for key in ("data", "result", "playurl"):
            sub = data.get(key)
            if isinstance(sub, dict):
                if "dash" in sub or "durl" in sub:
                    return sub

                for key2 in ("data", "result", "playurl"):
                    sub2 = sub.get(key2)
                    if isinstance(sub2, dict) and ("dash" in sub2 or "durl" in sub2):
                        return sub2

        return data

    def _build_stream_ladders(
        self,
        data: dict,
        *,
        max_quality: int = 64,
        allow_higher_fallback: bool = False,
        log_if_empty: bool = False,
    ) -> tuple[list[tuple[int, list[str]]], list[str]]:
        data = self._unwrap_playurl_data(data)

        durl = data.get("durl")
        if isinstance(durl, list) and durl:
            urls: list[str] = []

            for item in durl:
                if not isinstance(item, dict):
                    continue

                u = item.get("url")
                if isinstance(u, str) and u:
                    urls.append(u)

                backups = item.get("backup_url") or item.get("backupUrl") or []
                if isinstance(backups, list):
                    for bu in backups:
                        if isinstance(bu, str) and bu:
                            urls.append(bu)

            seen = set()
            uniq = []

            for u in urls:
                if u not in seen:
                    seen.add(u)
                    uniq.append(u)

            return ([(0, uniq)] if uniq else []), []

        dash = data.get("dash") or {}
        if not isinstance(dash, dict):
            msg = f"[Bilibili] playurl 无 dash/durl，keys={list(data.keys())}"
            if log_if_empty:
                logger.warning(msg)
            else:
                logger.debug(msg)
            return [], []

        video_streams = dash.get("video") or []
        audio_streams = dash.get("audio") or []

        if not audio_streams:
            dolby = dash.get("dolby") or {}
            if isinstance(dolby, dict):
                audio_streams = dolby.get("audio") or []

        if not audio_streams:
            flac = dash.get("flac") or {}
            if isinstance(flac, dict):
                audio = flac.get("audio")
                if isinstance(audio, dict):
                    audio_streams = [audio]
                elif isinstance(audio, list):
                    audio_streams = audio

        # === 关键修复：必须先定义 grouped ===
        grouped: dict[int, list[str]] = {}

        valid_video_streams = [
            v for v in video_streams
            if isinstance(v, dict)
            and self._detect_codec_type(v.get("codecs")) != "av1"
        ]

        # 按清晰度、编码偏好、码率排序。
        #
        # 排序逻辑：
        # 1. 清晰度越高越优先；
        # 2. 同清晰度下按 performance.video_codec 选择编码；
        # 3. 同编码下选择码率较低的流，降低体积和下载耗时；
        # 4. AV1 已过滤，不参与候选。
        valid_video_streams.sort(
            key=lambda v: (
                -int(v.get("id") or v.get("quality") or 0),
                self._codec_rank(v.get("codecs")),
                int(v.get("bandwidth") or 0),
            )
        )

        for v in valid_video_streams:
            qid = int(v.get("id") or v.get("quality") or 0)
            if qid <= 0:
                continue

            if (not allow_higher_fallback) and qid > max_quality:
                continue

            urls = self._collect_stream_urls(v)
            if not urls:
                continue

            grouped.setdefault(qid, [])

            for u in urls:
                if u not in grouped[qid]:
                    grouped[qid].append(u)

        video_ladders = sorted(grouped.items(), key=lambda x: x[0], reverse=True)

        audio_candidates: list[str] = []
        if isinstance(audio_streams, list) and audio_streams:
            for a in audio_streams:
                if not isinstance(a, dict):
                    continue

                urls = self._collect_stream_urls(a)
                if urls:
                    audio_candidates = urls
                    break

        if not video_ladders:
            dash_keys = list(dash.keys()) if isinstance(dash, dict) else []
            ids = []
            codecs_list = []

            for v in video_streams if isinstance(video_streams, list) else []:
                if isinstance(v, dict):
                    ids.append(v.get("id") or v.get("quality"))
                    codecs_list.append(v.get("codecs"))

            msg = (
                f"[Bilibili] 未识别到视频流: "
                f"top_keys={list(data.keys())}, dash_keys={dash_keys}, "
                f"video_count={len(video_streams) if isinstance(video_streams, list) else 'N/A'}, "
                f"video_ids={ids}, "
                f"video_codecs={codecs_list}, "
                f"audio_count={len(audio_streams) if isinstance(audio_streams, list) else 'N/A'}"
            )

            if log_if_empty:
                logger.warning(msg)
            else:
                logger.debug(msg)

        return video_ladders, audio_candidates

    @staticmethod
    def _quality_name(qid: int) -> str:
        mapping = {
            120: "4K",
            116: "1080P60",
            80: "1080P",
            74: "720P60",
            64: "720P",
            32: "480P",
            16: "360P",
            6: "240P",
            0: "单文件",
        }
        return mapping.get(qid, f"清晰度{qid}")

    async def _get_stream_ladders_with_qn_fallback(
        self,
        video: Video,
        page_index: int,
        cache_base_key: str,
    ) -> tuple[list[tuple[int, list[str]]], list[str], dict]:
        """
        B站 get_download_url 默认可能只返回当前/高画质。
        如果没拿到 <=720P，则按 qn=64/32/16/6 重新请求。
        """
        qn_order = [None, 64, 32, 16, 6]
        last_data: dict = {}

        for qn in qn_order:
            data = await self._get_playurl_cached(
                video,
                page_index,
                cache_base_key,
                qn=qn,
            )
            last_data = data

            ladders, audios = self._build_stream_ladders(
                data,
                max_quality=64,
                allow_higher_fallback=False,
                log_if_empty=False,
            )

            if ladders:
                logger.info(
                    f"[Bilibili] 获取到 <=720P 视频流 qn={qn or 'default'} "
                    f"qualities={[q for q, _ in ladders]}"
                )
                return ladders, audios, data

        # 极端兜底：如果 API 完全不给低清，只允许返回高画质，让后续 SizeLimit 处理
        ladders, audios = self._build_stream_ladders(
            last_data,
            max_quality=64,
            allow_higher_fallback=True,
            log_if_empty=True,
        )

        if ladders:
            logger.info(
                f"[Bilibili] 未拿到 <=720P，使用 API 返回画质兜底 qualities={[q for q, _ in ladders]}"
            )
            return ladders, audios, last_data

        return [], [], last_data

    # endregion

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        from .video import VideoInfo

        video = await self._get_video(bvid=bvid, avid=avid)

        try:
            key = f"bvid:{bvid}" if bvid else f"avid:{avid}"
            raw_info = await self._get_video_info_cached(video, key)
        except Exception as e:
            logger.error(f"[Bilibili] get_info error: {e}")
            raise ParseException(f"B站 API 请求失败: {e}")

        video_info = convert(raw_info, VideoInfo)
        page_info = video_info.extract_info_with_page(page_num)

        text = f"简介: {video_info.desc}" if video_info.desc else None
        author = self.create_author(video_info.owner.name, avatar_url=None)

        url = f"https://bilibili.com/{video_info.bvid}"
        url += f"?p={page_info.index + 1}" if page_info.index > 0 else ""

        # === B站评论区总开关 ===
        # 开启：发送阶段独立抓取并渲染评论区，不阻塞主视频解析返回
        # 关闭：直接跳过，减少请求和渲染耗时
        comment_task_factory = None
        if self.enable_comment_card:
            comment_task_factory = (
                lambda: self.comment_service.build_comment_image_content(
                    video_info.aid,
                    1,
                    video_title=page_info.title,
                    video_cover=self.norm_bili_img(page_info.cover),
                    video_author=video_info.owner.name,
                    video_timestamp=self.norm_bili_ts(page_info.timestamp),
                )
            )

        stream_task = self._get_stream_ladders_with_qn_fallback(
            video,
            page_info.index,
            f"{video_info.bvid}:{page_info.index}",
        )

        # 评论区抓取(最多翻 10 页 + 二维码识别)此前会卡住整个解析返回，
        # 现在只等取流即可返回，评论区改到发送阶段与视频并行处理。
        video_ladders, a_candidates, play_url_data = await stream_task

        if not video_ladders:
            logger.warning(
                f"[Bilibili] play_url_data keys="
                f"{list(play_url_data.keys()) if isinstance(play_url_data, dict) else type(play_url_data)}"
            )
            raise ParseException("B站未获取到可用视频流")

        async def download_video_task():
            output_path = self.cache_dir / f"{video_info.bvid}-{page_num}.mp4"

            if output_path.exists() and output_path.stat().st_size > 100:
                return output_path

            headers = self.headers.copy()
            headers["Referer"] = url

            last_err: Exception | None = None
            hit_size_limit = False

            for qid, v_urls in video_ladders:
                qname = self._quality_name(qid)
                logger.info(f"[Bilibili] 尝试下载 {video_info.bvid} P{page_num} {qname}")

                if a_candidates:
                    qid_size_limited = False

                    for v_url in v_urls:
                        for a_url in a_candidates:
                            try:
                                return await self.downloader.download_av_and_merge(
                                    v_url,
                                    a_url,
                                    output_path=output_path,
                                    ext_headers=headers,
                                    max_size_mb=self.max_size_mb,
                                )

                            except SizeLimitException as e:
                                last_err = e
                                hit_size_limit = True
                                qid_size_limited = True
                                logger.info(f"[Bilibili] {qname} 超过大小限制，降级尝试: {e}")
                                break

                            except Exception as e:
                                last_err = e
                                logger.warning(f"[Bilibili] {qname} CDN候选失败: {e}")
                                continue

                        if qid_size_limited:
                            break

                    continue

                for v_url in v_urls:
                    try:
                        return await self.downloader.streamd(
                            v_url,
                            file_name=output_path.name,
                            ext_headers=headers,
                            max_size_mb=self.max_size_mb,
                        )

                    except SizeLimitException as e:
                        last_err = e
                        hit_size_limit = True
                        logger.info(f"[Bilibili] {qname} 单文件超过大小限制，降级尝试: {e}")
                        break

                    except Exception as e:
                        last_err = e
                        logger.warning(f"[Bilibili] {qname} 单文件 CDN候选失败: {e}")
                        continue

            if hit_size_limit:
                raise SizeLimitException(
                    f"已从720P逐级降级尝试，最低可用清晰度仍超过限制 ({self.max_size_mb}MB)"
                )

            raise ParseException(f"B站媒体下载失败（已尝试全部候选）: {last_err}")

        video_content = self.create_video_content(
            asyncio.create_task(
                download_video_task(),
                name=f"bili_dl_{video_info.bvid}_{page_num}",
            ),
            cover_url=None,
            duration=page_info.duration,
        )
        video_content.is_file_upload = False

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=self.norm_bili_ts(page_info.timestamp),
            text=text,
            author=author,
            contents=[video_content],
            extra={"comment_task_factory": comment_task_factory} if comment_task_factory else {},
        )

    async def parse_dynamic(self, dynamic_id: int):
        return await self.dynamic_service.parse_dynamic(dynamic_id)

    async def parse_live(self, room_id: int):
        return await self.live_service.parse_live(room_id)

    async def _get_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
    ) -> Video:
        if avid:
            return Video(aid=avid, credential=await self.credential)

        if bvid:
            return Video(bvid=bvid, credential=await self.credential)

        raise ParseException("avid 和 bvid 至少指定一项")

    async def _init_credential(self):
        # 只初始化一次：cookie 缺失或加载失败时也不再重复尝试，
        # 避免每次解析视频都重新解析 cookie 字符串。
        self._credential_inited = True

        if not self.bili_ck:
            return

        try:
            self._credential = Credential.from_cookies(ck2dict(self.bili_ck))
        except Exception as e:
            logger.warning(f"Cookie加载失败: {e}")

    @property
    async def credential(self) -> Credential | None:
        if not self._credential_inited:
            await self._init_credential()

        return self._credential
