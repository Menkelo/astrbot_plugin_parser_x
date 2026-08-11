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
_AUDIO_HINTS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    "mime_type=audio",
    "ies-music",
    "music",
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


def extract_id_from_query(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
    except Exception:
        return None

    for key in ("modal_id", "aweme_id", "item_id", "video_id", "note_id", "id"):
        for value in query.get(key) or []:
            if value and str(value).isdigit():
                return str(value)

    path = parsed.path or ""

    match = re.search(r"/share/(?:video|note|slides)/(\d+)", path)
    if match:
        return match.group(1)

    match = re.search(r"/(?:video|note)/(\d+)", path)
    if match:
        return match.group(1)

    match = re.search(r"/(\d+)(?:/)?$", path)
    if match:
        return match.group(1)

    return None


def extract_router_data_json_str(html: str) -> str:
    match = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", html, re.DOTALL)
    if not match:
        raise ValueError("Router data not found in Douyin page")

    data = match.group(1).strip()
    if data.endswith(";"):
        data = data[:-1].strip()
    return data


def pick_primary_aweme(targets: list[dict], vid: str) -> dict:
    for obj in targets:
        aweme_id = str(obj.get("aweme_id") or obj.get("awemeId") or "")
        if aweme_id == vid:
            return obj
    return targets[0]


def _is_probably_audio_url(url: str) -> bool:
    if not isinstance(url, str) or not url:
        return False

    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return False

    return any(hint in low for hint in _AUDIO_HINTS)


def _is_probably_video_url(url: str) -> bool:
    if not isinstance(url, str) or not url:
        return False

    low = url.lower()
    if _is_probably_audio_url(low):
        return False

    return any(hint in low for hint in _VIDEO_HINTS)


def _is_probably_image_url(url: str) -> bool:
    if not isinstance(url, str) or not url:
        return False

    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return False
    if _is_probably_video_url(low):
        return False

    if any(ext in low for ext in _IMAGE_EXTS):
        return True

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
    return any(hint in low for hint in image_hints)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _score_image_url(url: str) -> int:
    low = url.lower()
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

    if any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp")):
        score += 5

    return score


def _collect_image_urls_from_obj(
    obj,
    path: tuple[str, ...] = (),
    depth: int = 0,
    max_depth: int = 8,
) -> list[str]:
    if depth > max_depth:
        return []

    urls: list[str] = []

    if isinstance(obj, str):
        if _is_probably_image_url(obj):
            joined_path = ".".join(path).lower()
            if not any(bad in joined_path for bad in _BAD_IMAGE_CONTEXT_KEYS):
                urls.append(obj)
        return urls

    if isinstance(obj, list):
        for index, item in enumerate(obj):
            urls.extend(
                _collect_image_urls_from_obj(
                    item,
                    path + (str(index),),
                    depth + 1,
                    max_depth,
                )
            )
        return urls

    if not isinstance(obj, dict):
        return urls

    for key, value in obj.items():
        key_name = str(key).lower()
        if key_name in _BAD_IMAGE_CONTEXT_KEYS:
            continue
        if key_name in {"video", "play_addr", "download_addr", "bit_rate", "play_url"}:
            continue

        next_path = path + (key_name,)
        if isinstance(value, str):
            if _is_probably_image_url(value):
                joined_path = ".".join(next_path).lower()
                if not any(bad in joined_path for bad in _BAD_IMAGE_CONTEXT_KEYS):
                    urls.append(value)
        elif isinstance(value, (list, dict)):
            urls.extend(
                _collect_image_urls_from_obj(
                    value,
                    next_path,
                    depth + 1,
                    max_depth,
                )
            )

    return urls


def extract_static_image_urls_deep(aweme_obj: dict) -> list[str]:
    if not isinstance(aweme_obj, dict):
        return []

    preferred_roots = []
    for key in ("images", "image_infos", "image_post_info"):
        value = aweme_obj.get(key)
        if value:
            preferred_roots.append((key, value))

    candidates: list[str] = []
    for key, root in preferred_roots:
        candidates.extend(_collect_image_urls_from_obj(root, path=(key,), max_depth=8))

    if not candidates:
        candidates.extend(
            _collect_image_urls_from_obj(aweme_obj, path=("aweme",), max_depth=8)
        )

    filtered = []
    for url in _dedupe_keep_order(candidates):
        low = url.lower()
        if "avatar" in low or "music" in low:
            continue
        if _is_probably_video_url(url):
            continue
        filtered.append(url)

    filtered = _dedupe_keep_order(filtered)
    filtered.sort(key=_score_image_url, reverse=True)
    return filtered
