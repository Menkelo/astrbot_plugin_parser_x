"""Shared media primitives; platform adapters decide which fields to inspect."""

from html import unescape
from urllib.parse import urlsplit

from ..data import DeliveryBatch, DeliveryPlan, DynamicContent, ImageContent


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


def motion_url(video: object) -> str | None:
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


def live_photo(parser, url: str, still: str | None, *, headers: dict) -> DynamicContent:
    fallback = None
    if still:
        fallback = ImageContent(
            parser.downloader.download_img(still, ext_headers=headers)
        )
    return DynamicContent(
        parser.downloader.download_video(url, ext_headers=headers),
        fallback_image=fallback,
    )


def ordered_media_plan(contents: list) -> DeliveryPlan:
    return (
        DeliveryPlan(
            [
                DeliveryBatch(
                    list(contents),
                    mode="forward" if len(contents) > 1 else "direct",
                    reply_original=len(contents) == 1
                    and isinstance(contents[0], ImageContent),
                )
            ]
        )
        if contents
        else DeliveryPlan()
    )
