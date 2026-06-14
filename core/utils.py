import asyncio
import base64
import hashlib
import inspect
import json
import mimetypes
import re
import shutil
from collections.abc import Awaitable, Callable
from collections import OrderedDict
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from astrbot.api import logger

K = TypeVar("K")
V = TypeVar("V")

IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"


async def maybe_close_session(session: Any) -> None:
    """兼容同步 / 异步 close 的 Session。"""
    if session is None:
        return

    ret = session.close()
    if inspect.isawaitable(ret):
        await ret


def normalize_image_url(url: str | None) -> str | None:
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


async def image_to_data_uri(
    http_get: Callable[..., Awaitable[Any]],
    img_url: str | None,
    *,
    headers: dict[str, str] | None = None,
    referer: str | None = None,
    normalizer: Callable[[str | None], str | None] | None = normalize_image_url,
    max_bytes: int = 4 * 1024 * 1024,
    timeout: int = 10,
    debug_label: str = "image",
) -> str | None:
    url = normalizer(img_url) if normalizer else img_url
    if not url:
        return None

    request_headers = dict(headers or {})
    request_headers["Accept"] = IMAGE_ACCEPT
    if referer:
        request_headers["Referer"] = referer

    try:
        resp = await http_get(
            url,
            headers=request_headers,
            allow_redirects=True,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            return None

        content = resp.content or b""
        if not content or len(content) > max_bytes:
            return None

        content_type = (resp.headers.get("Content-Type", "") or "").split(";", 1)[0].strip()
        if not content_type.lower().startswith("image/"):
            guessed, _ = mimetypes.guess_type(urlparse(url).path)
            content_type = guessed or "image/jpeg"

        if not content_type.lower().startswith("image/"):
            content_type = "image/jpeg"

        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    except Exception as e:
        logger.debug(f"{debug_label} data URI failed: {url} | {e}")
        return None


class LimitedSizeDict(OrderedDict[K, V]):
    def __init__(self, *args, max_size=128, **kwargs):
        self.max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: K, value: V):
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)


async def safe_unlink(path: Path):
    """
    安全删除文件或目录。
    """
    try:
        if path.is_dir():
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
        else:
            await asyncio.to_thread(path.unlink, missing_ok=True)
    except Exception as e:
        logger.warning(f"删除 {path} 失败: {e}")


async def exec_ffmpeg_cmd(cmd: list[str]) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        return_code = process.returncode
    except FileNotFoundError:
        raise RuntimeError("ffmpeg 未安装或无法找到可执行文件")

    if return_code != 0:
        error_msg = stderr.decode(errors="ignore").strip()
        logger.warning(f"ffmpeg 错误: {error_msg}")
        raise RuntimeError(f"ffmpeg 执行失败: {error_msg}")


async def merge_av(v_path: Path, a_path: Path, output_path: Path) -> None:
    """
    极速合并音视频：使用流复制 Stream Copy。
    """
    if not v_path.exists() or v_path.stat().st_size == 0:
        raise FileNotFoundError(f"视频源文件缺失或为空: {v_path}")

    if not a_path.exists() or a_path.stat().st_size == 0:
        raise FileNotFoundError(f"音频源文件缺失或为空: {a_path}")

    logger.debug(f"⚡ 极速合并: {v_path.name} + {a_path.name}")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c",
        "copy",
        "-strict",
        "experimental",
        str(output_path),
    ]
    await exec_ffmpeg_cmd(cmd)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg 合并未生成有效文件")

    await asyncio.gather(safe_unlink(v_path), safe_unlink(a_path))
    logger.debug(f"✅ 合并完成: {output_path.name} ({fmt_size(output_path)})")


def fmt_size(file_path: Path) -> str:
    try:
        return f"{file_path.stat().st_size / 1024 / 1024:.2f} MB"
    except Exception:
        return "未知大小"


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """
    清洗文件名，兼容 Windows/Linux。
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = name.strip().strip(".")
    if not name:
        name = "file"

    if len(name) > max_len:
        name = name[:max_len].rstrip("._- ")

    return name or "file"


def generate_file_name(url: str, suffix: str | None = None) -> str:
    """
    根据 URL 生成稳定、安全、低冲突的缓存文件名。

    特点：
    - 使用 URL path 中的文件名；
    - 自动清洗非法字符；
    - 自动追加 URL hash，避免同名覆盖；
    - 支持强制后缀。
    """
    digest = hashlib.md5(url.encode()).hexdigest()[:8]

    try:
        parsed = urlparse(url)
        name = Path(parsed.path).name

        if "%" in name:
            import urllib.parse

            name = urllib.parse.unquote(name)

        if not name or name == "/":
            name = f"file_{digest}"

        path_obj = Path(name)
        stem = path_obj.stem or "file"
        ext = path_obj.suffix

        stem = sanitize_filename(stem)

        if suffix:
            ext = suffix
        elif not ext:
            ext = ".unknown"

        ext = sanitize_filename(ext, max_len=16)
        if ext and not ext.startswith("."):
            ext = "." + ext

        return f"{stem}_{digest}{ext}"

    except Exception:
        return f"{digest}{suffix or '.unknown'}"


def ck2dict(cookies_str: str) -> dict[str, str]:
    res = {}
    if not cookies_str:
        return res

    for cookie in cookies_str.split(";"):
        if "=" in cookie:
            name, value = cookie.strip().split("=", 1)
            res[name] = value

    return res


def extract_json_url(data: dict | str) -> str | None:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None

    if not isinstance(data, dict):
        return None

    meta: dict[str, Any] | None = data.get("meta")
    if meta:
        keys_to_check = [
            ("detail_1", "qqdocurl"),
            ("detail_1", "desc"),
            ("news", "jumpUrl"),
            ("music", "jumpUrl"),
            ("music", "musicUrl"),
        ]

        for key1, key2 in keys_to_check:
            val = meta.get(key1, {}).get(key2)
            if val and isinstance(val, str):
                val = val.replace("&amp;", "&")
                if "http" in val:
                    if m := re.search(r'https?://[^\s,"]+', val):
                        return m.group(0)

    found_url = _recursive_find_xhs_url(data)
    if found_url:
        return found_url.replace("&amp;", "&")

    return None


def _recursive_find_xhs_url(obj: Any) -> str | None:
    if isinstance(obj, str):
        if "xhslink.com" in obj or "xiaohongshu.com" in obj:
            if m := re.search(
                r'https?://[a-zA-Z0-9./?=&_%#@:-]*(?:xhslink\.com|xiaohongshu\.com)[a-zA-Z0-9./?=&_%#@:-]*',
                obj,
            ):
                return m.group(0)
        return None

    if isinstance(obj, dict):
        for v in obj.values():
            if res := _recursive_find_xhs_url(v):
                return res

    if isinstance(obj, list):
        for v in obj:
            if res := _recursive_find_xhs_url(v):
                return res

    return None
