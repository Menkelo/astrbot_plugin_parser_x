import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from msgspec import convert

from astrbot.api import logger

from ...data import VideoContent
from ...exception import SizeLimitException
from ..base import ParseException

if TYPE_CHECKING:
    from . import BilibiliParser


class BiliVideoService:
    def __init__(self, parser: "BilibiliParser"):
        self.parser = parser

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        from .video import VideoInfo

        parser = self.parser

        video = await parser._get_video(bvid=bvid, avid=avid)

        try:
            key = f"bvid:{bvid}" if bvid else f"avid:{avid}"
            raw_info = await parser._get_video_info_cached(video, key)
        except Exception as e:
            logger.error(f"[Bilibili] get_info error: {e}")
            raise ParseException(f"B站 API 请求失败: {e}")

        video_info = convert(raw_info, VideoInfo)
        page_info = video_info.extract_info_with_page(page_num)

        text = f"简介: {video_info.desc}" if video_info.desc else None
        author = parser.create_author(video_info.owner.name, avatar_url=None)

        url = f"https://bilibili.com/{video_info.bvid}"
        url += f"?p={page_info.index + 1}" if page_info.index > 0 else ""

        task_play_url = parser._get_playurl_cached(
            video,
            page_info.index,
            f"{video_info.bvid}:{page_info.index}",
        )

        task_comments = parser.comment_service.build_comment_image_content(
            video_info.aid,
            1,
            video_title=page_info.title,
            video_cover=parser._norm_bili_img(page_info.cover),
            video_author=video_info.owner.name,
            video_timestamp=parser._norm_bili_ts(page_info.timestamp),
        )

        play_url_data, comment_imgs = await asyncio.gather(task_play_url, task_comments)

        v_candidates, a_candidates = parser._select_best_stream_candidates(
            play_url_data,
            page_info.duration,
            parser.max_size_mb,
        )

        if not v_candidates:
            raise SizeLimitException(f"即使是最低画质也超过了限制 ({parser.max_size_mb}MB)")

        async def download_video_task():
            output_path = parser.cache_dir / f"{video_info.bvid}-{page_num}.mp4"

            if output_path.exists() and output_path.stat().st_size > 100:
                return output_path

            headers = parser.headers.copy()
            headers["Referer"] = url
            last_err: Exception | None = None

            if a_candidates:
                for v_url in v_candidates:
                    for a_url in a_candidates:
                        try:
                            return await parser.downloader.download_av_and_merge(
                                v_url,
                                a_url,
                                output_path=output_path,
                                ext_headers=headers,
                                max_size_mb=parser.max_size_mb,
                            )
                        except Exception as e:
                            last_err = e

            for v_url in v_candidates:
                try:
                    return await parser.downloader.streamd(
                        v_url,
                        file_name=output_path.name,
                        ext_headers=headers,
                        max_size_mb=parser.max_size_mb,
                    )
                except Exception as e:
                    last_err = e

            if isinstance(last_err, SizeLimitException):
                raise last_err

            raise ParseException(f"B站媒体下载失败（已尝试全部CDN候选）: {last_err}")

        video_content = parser.create_video_content(
            asyncio.create_task(
                download_video_task(),
                name=f"bili_dl_{video_info.bvid}_{page_num}",
            ),
            cover_url=None,
            duration=page_info.duration,
        )
        video_content.is_file_upload = False

        return parser.result(
            url=url,
            title=page_info.title,
            timestamp=parser._norm_bili_ts(page_info.timestamp),
            text=text,
            author=author,
            contents=[video_content],
            comment_contents=comment_imgs,
        )
