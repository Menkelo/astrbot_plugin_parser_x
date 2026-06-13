import re
from urllib.parse import parse_qs, urlparse


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic")
_VIDEO_HINTS = (
    ".mp4",
    ".m3u8",
    "video_id=",
    "mime_type=video",
    "/video/",
    "playwm",
    "aweme/v1/play",
    "api-play",
    "is_play_url=1",
)

_BAD_IMAGE_CONTEXT_KEYS = {
    "author",
    "avatar",
    "avatar_thumb",
    "avatar_medium",
    "avatar_larger",
    "music",
    "cover_hd",
    "music_cover",
    "share_info",
    "risk_infos",
    "statistics",
    "status",
}

_IMAGE_CONTEXT_KEYS = {
    "images",
    "image",
    "image_infos",
    "image_post_info",
    "display_image",
    "origin_cover",
    "cover",
    "cover_url",
    "large",
    "medium",
    "thumbnail",
    "thumb",
    "url_list",
    "download_url_list",
}


def extract_id_from_query(url: str) -> str | None:
    try:
        p = urlparse(url)
        query = parse_qs(p.query)
    except Exception:
        return None

    for key in ("modal_id", "aweme_id", "item_id", "video_id", "note_id", "id"):
        vals = query.get(key) or []
        for v in vals:
            if v and str(v).isdigit():
                return str(v)

    path = p.path or ""

    m = re.search(r"/share/(?:video|note|slides)/(\d+)", path)
    if m:
        return m.group(1)

    m = re.search(r"/(?:video|note)/(\d+)", path)
    if m:
        return m.group(1)

    m = re.search(r"/(\d+)(?:/)?$", path)
    if m:
        return m.group(1)

    return None


def extract_router_data_json_str(html: str) -> str:
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", html, re.DOTALL)
    if not m:
        raise ValueError("未在页面 HTML 中找到 _ROUTER_DATA")
    s = m.group(1).strip()
    if s.endswith(";"):
        s = s[:-1].strip()
    return s


def pick_primary_aweme(targets: list[dict], vid: str) -> dict:
    for obj in targets:
        oid = str(obj.get("aweme_id") or obj.get("awemeId") or "")
        if oid == vid:
            return obj
    return targets[0]


def _is_probably_video_url(u: str) -> bool:
    if not isinstance(u, str) or not u:
        return False

    low = u.lower()
    return any(h in low for h in _VIDEO_HINTS)


def _is_probably_image_url(u: str) -> bool:
    if not isinstance(u, str) or not u:
        return False

    low = u.lower()

    if not low.startswith(("http://", "https://")):
        return False

    if _is_probably_video_url(low):
        return False

    if any(ext in low for ext in _IMAGE_EXTS):
        return True

    # 抖音/字节图片 CDN 有些 URL 不带扩展名
    image_hints = (
        "image",
        "img",
        "tos-cn",
        "byteimg",
        "douyinpic",
        "p3-",
        "p6-",
        "p9-",
        "p11-",
        "p26-",
        "p-pc-sign",
    )
    return any(h in low for h in image_hints)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in items:
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out

def _as_url_list(v) -> list[str]:
    """
    兼容抖音常见 URL 字段结构：
    - str
    - list[str]
    - {"url_list": [...]}
    - {"urlList": [...]}
    - {"uri": "...", "url_list": [...]}
    """
    out: list[str] = []

    if isinstance(v, str):
        if v:
            out.append(v)
        return out

    if isinstance(v, list):
        for x in v:
            if isinstance(x, str) and x:
                out.append(x)
            elif isinstance(x, dict):
                out.extend(_as_url_list(x))
        return out

    if isinstance(v, dict):
        for key in (
            "url_list",
            "urlList",
            "urls",
            "url",
            "uri",
            "download_url_list",
            "downloadUrlList",
        ):
            val = v.get(key)
            if val:
                out.extend(_as_url_list(val))

    return _dedupe_keep_order(out)


def _normalize_douyin_video_url(u: str) -> str:
    return u.replace("playwm", "play")


def _pick_best_image_url(urls: list[str]) -> str | None:
    urls = _dedupe_keep_order([u for u in urls if _is_probably_image_url(u)])
    if not urls:
        return None

    urls.sort(key=_score_image_url, reverse=True)
    return urls[0]


def _collect_static_urls_from_image_node(img: dict) -> list[str]:
    """
    只从“更像作品静图”的字段里拿图，避免把 video.cover / avatar / music cover 当成静图。
    """
    if not isinstance(img, dict):
        return []

    candidates: list[str] = []

    # 抖音图文最常见字段：image.url_list
    for key in (
        "url_list",
        "urlList",
        "download_url_list",
        "downloadUrlList",
        "origin_url_list",
        "originUrlList",
        "large_url_list",
        "largeUrlList",
        "display_url_list",
        "displayUrlList",
    ):
        if key in img:
            candidates.extend(_as_url_list(img.get(key)))

    # 有些结构会放在这些子节点里
    for key in (
        "display_image",
        "origin_cover",
        "cover",
        "large",
        "medium",
        "thumbnail",
        "thumb",
        "image",
    ):
        val = img.get(key)
        if isinstance(val, dict):
            # 如果这个子节点里面包含 video，就不要深扫 video 里的 cover
            copied = dict(val)
            copied.pop("video", None)
            copied.pop("play_addr", None)
            copied.pop("download_addr", None)
            candidates.extend(_collect_image_urls_from_obj(copied, path=("images", key), max_depth=4))
        else:
            candidates.extend(_as_url_list(val))

    filtered: list[str] = []
    for u in candidates:
        if not isinstance(u, str) or not u:
            continue

        low = u.lower()

        if _is_probably_video_url(u):
            continue

        # 排除明显不是作品图的资源
        if any(bad in low for bad in ("avatar", "music", "user/profile")):
            continue

        if _is_probably_image_url(u):
            filtered.append(u)

    return _dedupe_keep_order(filtered)


def _collect_video_urls_from_image_node(img: dict) -> list[str]:
    """
    从单个 images 节点里提取动图视频 URL。
    兼容 play_addr / download_addr / play_url 等不同字段。
    """
    if not isinstance(img, dict):
        return []

    video = img.get("video")
    if not isinstance(video, dict):
        return []

    candidates: list[str] = []

    for key in (
        "play_addr",
        "playAddr",
        "download_addr",
        "downloadAddr",
        "play_url",
        "playUrl",
        "bit_rate",
        "bitRate",
    ):
        val = video.get(key)

        if isinstance(val, list):
            for item in val:
                candidates.extend(_as_url_list(item))
        else:
            candidates.extend(_as_url_list(val))

    # 兜底深扫 video 节点，但是只保留明显视频 URL
    if not candidates:
        candidates.extend(_collect_video_urls_deep(video))

    out: list[str] = []
    for u in candidates:
        if not isinstance(u, str) or not u:
            continue

        nu = _normalize_douyin_video_url(u)

        if _is_probably_video_url(nu) or "douyin" in nu.lower() or "byte" in nu.lower():
            out.append(nu)

    return _dedupe_keep_order(out)


def _collect_video_urls_deep(obj, depth: int = 0, max_depth: int = 6) -> list[str]:
    if depth > max_depth:
        return []

    urls: list[str] = []

    if isinstance(obj, str):
        if _is_probably_video_url(obj):
            urls.append(obj)
        return urls

    if isinstance(obj, list):
        for it in obj:
            urls.extend(_collect_video_urls_deep(it, depth + 1, max_depth))
        return urls

    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()

            # 动图视频常见字段优先
            if lk in {
                "play_addr",
                "playaddr",
                "download_addr",
                "downloadaddr",
                "play_url",
                "playurl",
                "url_list",
                "urllist",
                "download_url_list",
                "downloadurllist",
            }:
                for u in _as_url_list(v):
                    if _is_probably_video_url(u):
                        urls.append(u)

            urls.extend(_collect_video_urls_deep(v, depth + 1, max_depth))

    return _dedupe_keep_order(urls)

def _score_image_url(u: str) -> int:
    low = u.lower()
    score = 0

    if "origin" in low:
        score += 40
    if "large" in low:
        score += 35
    if "display" in low:
        score += 25
    if "image" in low or "img" in low:
        score += 10
    if "tos-cn" in low or "byteimg" in low or "douyinpic" in low:
        score += 10

    if "thumb" in low or "thumbnail" in low:
        score -= 10
    if "avatar" in low:
        score -= 100
    if "music" in low:
        score -= 80

    if ".jpg" in low or ".jpeg" in low or ".png" in low or ".webp" in low:
        score += 5

    return score


def _extract_dynamic_video_from_image(img: dict, idx: int) -> tuple[str, str] | None:
    """
    从单个 images 节点提取动图视频。
    """
    if not isinstance(img, dict):
        return None

    urls = _collect_video_urls_from_image_node(img)
    if not urls:
        return None

    url = urls[0]

    video = img.get("video") or {}
    play_addr = {}
    if isinstance(video, dict):
        play_addr = video.get("play_addr") or video.get("playAddr") or {}

    uri = None
    if isinstance(play_addr, dict):
        uri = play_addr.get("uri")

    if uri:
        key = f"uri:{uri}"
    else:
        q = parse_qs(urlparse(url).query)
        video_id = (q.get("video_id") or q.get("vid") or q.get("item_id") or [""])[0]
        key = f"vid:{video_id}" if video_id else f"idx:{idx}:{url}"

    return key, url


def _collect_image_urls_from_obj(
    obj,
    path: tuple[str, ...] = (),
    depth: int = 0,
    max_depth: int = 8,
) -> list[str]:
    """
    从任意对象递归收集图片 URL。

    带路径过滤：
    - 跳过 avatar/music/share/video 等无关上下文；
    - 优先收集 images/image_post_info/image_infos 里的图。
    """
    if depth > max_depth:
        return []

    urls: list[str] = []

    if isinstance(obj, str):
        if _is_probably_image_url(obj):
            joined = ".".join(path).lower()
            if not any(bad in joined for bad in _BAD_IMAGE_CONTEXT_KEYS):
                urls.append(obj)
        return urls

    if isinstance(obj, list):
        for i, it in enumerate(obj):
            urls.extend(_collect_image_urls_from_obj(it, path + (str(i),), depth + 1, max_depth))
        return urls

    if not isinstance(obj, dict):
        return urls

    for k, v in obj.items():
        lk = str(k).lower()

        # 这些分支强跳过，避免收头像、音乐封面、分享图、视频播放地址
        if lk in _BAD_IMAGE_CONTEXT_KEYS:
            continue
        if lk in {"video", "play_addr", "download_addr", "bit_rate", "play_url"}:
            continue

        new_path = path + (lk,)

        if isinstance(v, str):
            if _is_probably_image_url(v):
                joined = ".".join(new_path).lower()
                if not any(bad in joined for bad in _BAD_IMAGE_CONTEXT_KEYS):
                    urls.append(v)

        elif isinstance(v, list):
            urls.extend(_collect_image_urls_from_obj(v, new_path, depth + 1, max_depth))

        elif isinstance(v, dict):
            urls.extend(_collect_image_urls_from_obj(v, new_path, depth + 1, max_depth))

    return urls


def _extract_static_image_from_image(img: dict, idx: int) -> tuple[str, str] | None:
    """
    从单个 images 节点提取作品静态图。

    注意：
    - 不从 video 节点里取 cover，避免把动图封面误判为静图；
    - 优先取当前 image 节点自身的 url_list/download_url_list 等。
    """
    if not isinstance(img, dict):
        return None

    candidates = _collect_static_urls_from_image_node(img)
    image_url = _pick_best_image_url(candidates)

    if not image_url:
        return None

    return f"idx:{idx}:{image_url}", image_url


def extract_static_image_urls_deep(aweme_obj: dict) -> list[str]:
    """
    从整个 aweme 对象递归提取静态图 URL。
    用于 images 节点提取不到静态图时兜底。
    """
    if not isinstance(aweme_obj, dict):
        return []

    preferred_roots = []

    # 优先常见图文结构
    for key in ("images", "image_infos", "image_post_info"):
        v = aweme_obj.get(key)
        if v:
            preferred_roots.append((key, v))

    candidates: list[str] = []

    for key, root in preferred_roots:
        candidates.extend(_collect_image_urls_from_obj(root, path=(key,), max_depth=8))

    # 如果常见结构没有，再扫整个 aweme
    if not candidates:
        candidates.extend(_collect_image_urls_from_obj(aweme_obj, path=("aweme",), max_depth=8))

    candidates = _dedupe_keep_order(candidates)

    # 过滤明显不是作品图片的
    filtered = []
    for u in candidates:
        low = u.lower()
        if "avatar" in low or "music" in low:
            continue
        if _is_probably_video_url(u):
            continue
        filtered.append(u)

    filtered = _dedupe_keep_order(filtered)
    filtered.sort(key=_score_image_url, reverse=True)

    return filtered


def extract_mixed_image_dynamic_items(aweme_obj: dict) -> list[tuple[str, str, str]]:
    """
    返回:
    [
        ("image", key, image_url),
        ("video", key, video_url),
    ]

    修复点：
    - 每个 images 节点同时尝试提取静图和动图；
    - 不因为提取到了动图就跳过静图；
    - 不因为提取到了静图就跳过动图；
    - 如果其中一种缺失，再做全局兜底。
    """
    items: list[tuple[str, str, str]] = []

    if not isinstance(aweme_obj, dict):
        return items

    images = aweme_obj.get("images") or []
    seen: set[str] = set()

    if isinstance(images, list):
        for idx, img in enumerate(images):
            if not isinstance(img, dict):
                continue

            # 1. 静图
            static = _extract_static_image_from_image(img, idx)
            if static:
                key, image_url = static
                final_key = f"image:{image_url}"
                if final_key not in seen:
                    seen.add(final_key)
                    items.append(("image", key, image_url))

            # 2. 动图
            dynamic = _extract_dynamic_video_from_image(img, idx)
            if dynamic:
                key, video_url = dynamic
                final_key = f"video:{key}"
                if final_key not in seen:
                    seen.add(final_key)
                    items.append(("video", key, video_url))

    has_video = any(t == "video" for t, _, _ in items)
    has_image = any(t == "image" for t, _, _ in items)

    # 静图缺失兜底：深扫作品静图
    if not has_image:
        deep_images = extract_static_image_urls_deep(aweme_obj)
        for i, image_url in enumerate(deep_images):
            final_key = f"image:{image_url}"
            if final_key in seen:
                continue

            seen.add(final_key)
            items.insert(i, ("image", f"deep:{i}:{image_url}", image_url))

    # 动图缺失兜底：扫 images.video
    if not has_video and isinstance(images, list):
        insert_tail: list[tuple[str, str, str]] = []

        for idx, img in enumerate(images):
            dynamic = _extract_dynamic_video_from_image(img, idx)
            if not dynamic:
                continue

            key, video_url = dynamic
            final_key = f"video:{key}"
            if final_key in seen:
                continue

            seen.add(final_key)
            insert_tail.append(("video", key, video_url))

        items.extend(insert_tail)

    return items


def extract_dynamic_video_entries_with_index(aweme_obj: dict) -> list[tuple[int, str, str]]:
    entries: list[tuple[int, str, str]] = []
    images = aweme_obj.get("images") or []
    if not isinstance(images, list):
        return entries

    seen: set[str] = set()
    for idx, img in enumerate(images):
        dynamic = _extract_dynamic_video_from_image(img, idx)
        if not dynamic:
            continue

        key, url = dynamic
        if key in seen:
            continue
        seen.add(key)
        entries.append((idx, key, url))

    return entries


def extract_dynamic_video_entries(aweme_obj: dict) -> list[tuple[str, str]]:
    return [(key, url) for _, key, url in extract_dynamic_video_entries_with_index(aweme_obj)]


def extract_static_image_urls_excluding_dynamic(aweme_obj: dict, dynamic_indexes: set[int]) -> list[str]:
    return extract_static_image_urls_deep(aweme_obj)


def extract_bgm_url(aweme_obj: dict) -> str | None:
    music = aweme_obj.get("music") or {}
    if not isinstance(music, dict):
        return None

    play_url = music.get("play_url") or {}
    if not isinstance(play_url, dict):
        return None

    url_list = play_url.get("url_list") or []
    if not isinstance(url_list, list):
        return None

    for u in url_list:
        if isinstance(u, str) and u:
            return u

    return None
