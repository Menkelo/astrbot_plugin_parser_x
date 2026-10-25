"""Shared media primitives; platform adapters decide which fields to inspect."""

from html import unescape
from urllib.parse import urlsplit

from ..data import DeliveryBatch, DeliveryPlan, DynamicContent, ImageContent
from ..video_codec import normalize_codec, prefer_candidates


def media_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = unescape(value.strip())
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return value
    except ValueError:
        pass
    return None


def first_media_url(value: object) -> str | None:
    if url := media_url(value):
        return url
    if isinstance(value, (list, tuple)):
        for item in value:
            if url := first_media_url(item):
                return url
    elif isinstance(value, dict):
        for key in (
            "masterUrl",
            "master_url",
            "url",
            "url_list",
            "backupUrls",
            "backup_urls",
        ):
            if url := first_media_url(value.get(key)):
                return url
    return None


def _source_codec(item: dict) -> str:
    """Only declared metadata is evidence; CDN names/URL text are not codecs."""
    for key in ("codec", "codec_type", "vcodec", "video_codec"):
        if codec := normalize_codec(item.get(key)):
            return codec
    if item.get("is_h265") in (1, True, "1"):
        return "hevc"
    if "is_h265" in item and item["is_h265"] in (0, False, "0"):
        return "avc"
    return ""


def preferred_motion_url(video: object, preference: str) -> str | None:
    """Return a known target representation, or None to retain caller's default."""
    if preference not in {"avc", "hevc"} or not isinstance(video, dict):
        return None
    candidates = []
    streams = video.get("stream")
    if isinstance(streams, dict):
        for key in ("h264", "h265", "h266", "av1"):
            if url := first_media_url(streams.get(key)):
                candidates.append((url, normalize_codec(key)))
    for key, codec in (
        ("play_addr_h264", "avc"), ("play_addr", _source_codec(video)),
        ("play_addr_265", "hevc"), ("play_addr_256", ""),
        ("download_addr", ""), ("video_play_addr", ""),
    ):
        value = video.get(key)
        if url := first_media_url(value):
            evidence = _source_codec(value) if isinstance(value, dict) else ""
            candidates.append((url.replace("/playwm/", "/play/"), evidence or codec))
    rates = video.get("bit_rate")
    for rate in rates if isinstance(rates, list) else []:
        if isinstance(rate, dict) and (url := first_media_url(rate.get("play_addr"))):
            address = rate["play_addr"]
            evidence = _source_codec(address) if isinstance(address, dict) else ""
            candidates.append((url, evidence or _source_codec(rate)))
    selected = prefer_candidates(candidates, preference, lambda item: item[1])
    return next((url for url, codec in selected if codec == preference), None)


def motion_url(video: object, preference: str = "auto") -> str | None:
    if preferred := preferred_motion_url(video, preference):
        return preferred
    if not isinstance(video, dict):
        return media_url(video)
    streams = video.get("stream")
    if isinstance(streams, dict):
        for codec in ("h264", "h265", "h266", "av1"):
            if url := first_media_url(streams.get(codec)):
                return url
    for key in (
        "play_addr_h264",
        "play_addr",
        "play_addr_265",
        "play_addr_256",
        "download_addr",
        "video_play_addr",
    ):
        if url := first_media_url(video.get(key)):
            return url.replace("/playwm/", "/play/")
    rates = video.get("bit_rate")
    if isinstance(rates, list):
        for rate in rates:
            if isinstance(rate, dict) and (
                url := first_media_url(rate.get("play_addr"))
            ):
                return url
    return None


def live_photo(
    parser, url: str, still: str | None, *, headers: dict
) -> ImageContent | DynamicContent:
    """实况图统一按静态图返回。

    QQ 侧（OneBot / NapCat / snowluma）没有实况图消息类型，动态片段发出去只会
    变成一段独立短视频，和静态图配不成一对，用户看到的仍是"静态图"。
    因此有静态图时只下载静态图，不再下载动态片段。
    确实没有静态图时才退回动态片段，避免整张媒体丢失。
    """
    if still:
        return ImageContent(
            parser.downloader.download_img(still, ext_headers=headers)
        )
    return DynamicContent(
        parser.downloader.download_video(url, ext_headers=headers),
    )


def ordered_media_plan(contents: list) -> DeliveryPlan:
    return (
        DeliveryPlan(
            [
                DeliveryBatch(
                    list(contents),
                    # 无论单图还是多图一律进合并转发，让「正文卡 + 图片」
                    # 作为同一条转发出现，而不是卡片与图片各发一条。
                    mode="forward",
                    reply_original=False,
                )
            ]
        )
        if contents
        else DeliveryPlan()
    )
