"""Resolve trusted Coolapk card media through the existing download cache."""
import asyncio
import base64
from pathlib import Path
from .download import _detect_image_suffix
from .parsers.coolapk import _image_url


def _data_uri(path):
    path = Path(path)
    suffix = _detect_image_suffix(path)
    mime = {'.jpg':'image/jpeg', '.png':'image/png', '.gif':'image/gif',
            '.webp':'image/webp', '.apng':'image/apng'}.get(suffix)
    if mime is None or not 0 < path.stat().st_size <= 100 * 1024 * 1024:
        raise ValueError('酷安卡片图片无效或超过大小上限')
    return 'data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


async def local_coolapk_images(result, downloader, blocks, media_url, avatar):
    """Do not let Chromium silently hide anti-hotlink failures.

    Source image tasks are already single-flight downloads; reuse their exact
    paths. Only profile/cover images need additional trusted-host downloads.
    """
    flow = list(blocks or [])
    urls = [b.get('url') for b in flow if isinstance(b, dict) and b.get('type') == 'image']
    local = {}
    if urls:
        sources = result.extra.get('text_card_source_images', urls)
        paths = await asyncio.gather(*(c.get_path() for c in result.contents))
        if len(paths) != len(sources) or not set(urls) <= set(sources):
            raise ValueError('酷安正文图片与下载任务不匹配')
        for url, path in zip(sources, paths):
            local[url] = await asyncio.to_thread(_data_uri, path)
    async def extra_image(url, hosts):
        if not url:
            return None
        if url in local:
            return local[url]
        safe = _image_url(url, hosts)
        if not safe:
            return None
        path = await downloader.download_img(safe)
        return await asyncio.to_thread(_data_uri, path)
    media_url = await extra_image(media_url, {'image.coolapk.com'})
    # Missing avatar must not discard an otherwise complete article.
    try:
        avatar = await extra_image(avatar, {'avatar.coolapk.com'})
    except Exception:
        avatar = None
    return [dict(b, url=local[b['url']]) if isinstance(b, dict) and b.get('type') == 'image' else b for b in flow], media_url, avatar
