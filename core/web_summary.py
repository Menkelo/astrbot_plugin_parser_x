from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import aiohttp

from .exception import ParseException

BLOCKED_PLATFORM_HOSTS = {
    "instagram.com",
    "itunes.apple.com",
    "music.apple.com",
    "open.spotify.com",
    "spotify.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtu.be",
    "youtube.com",
}
PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def is_blocked_platform_host(hostname: str | None) -> bool:
    host = (hostname or "").strip(".").lower()
    return any(
        host == item or host.endswith(f".{item}") for item in BLOCKED_PLATFORM_HOSTS
    )


async def validate_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ParseException("链接必须是有效的 HTTP/HTTPS 公网地址")
    if parsed.username or parsed.password:
        raise ParseException("链接不能包含用户名或密码")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ParseException("链接端口无效") from exc
    if port not in (None, 80, 443):
        raise ParseException("链接端口不在允许范围内")
    if is_blocked_platform_host(parsed.hostname):
        raise ParseException("该国外平台不在 Parser X 的支持范围内")
    host = parsed.hostname.strip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ParseException("不允许访问本机或局域网地址")

    try:
        direct_ip = ipaddress.ip_address(host)
        addresses = {direct_ip}
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ParseException(f"域名解析失败: {exc}") from exc
        addresses = {ipaddress.ip_address(item[4][0]) for item in records}

    if not addresses or any(
        not address.is_global
        and (is_literal_ip or address not in PROXY_FAKE_IP_NETWORK)
        for address in addresses
    ):
        raise ParseException("不允许访问本机、内网或保留地址")
    return parsed.geturl()


class _ReadableTextParser(HTMLParser):
    ignored_tags = {
        "script",
        "style",
        "noscript",
        "svg",
        "nav",
        "footer",
        "header",
        "form",
    }
    block_tags = {
        "article",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag in self.ignored_tags:
            self.ignored_depth += 1
        if tag == "title":
            self.in_title = True
        if not self.ignored_depth and tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag in self.ignored_tags and self.ignored_depth:
            self.ignored_depth -= 1
        if not self.ignored_depth and tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self.ignored_depth:
            return
        value = re.sub(r"\s+", " ", unescape(data)).strip()
        if not value:
            return
        if self.in_title:
            self.title_parts.append(value)
        self.parts.append(value)

    def result(self, max_chars: int) -> tuple[str, str]:
        title = " ".join(self.title_parts).strip()
        raw = " ".join(self.parts)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        text = raw.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + "…"
        return title, text


def extract_readable_text(html: str, max_chars: int = 12000) -> tuple[str, str]:
    parser = _ReadableTextParser()
    parser.feed(html or "")
    return parser.result(max(1000, max_chars))


@dataclass(slots=True)
class WebPageContent:
    url: str
    title: str
    text: str


async def fetch_public_page(
    url: str,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    max_chars: int = 12000,
) -> WebPageContent:
    current = await validate_public_url(url)
    timeout = aiohttp.ClientTimeout(total=25, connect=10, sock_read=15)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for _ in range(6):
            async with session.get(current, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ParseException("网页重定向缺少目标地址")
                    current = await validate_public_url(urljoin(current, location))
                    continue
                if response.status >= 400:
                    raise ParseException(f"网页请求失败: HTTP {response.status}")
                content_type = response.headers.get("Content-Type", "").lower()
                if not any(
                    item in content_type for item in ("text/html", "text/plain")
                ):
                    raise ParseException("链接目标不是可总结的网页文本")
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length and content_length > max_bytes:
                    raise ParseException("网页内容超过允许大小")
                chunks = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ParseException("网页内容超过允许大小")
                    chunks.append(chunk)
                charset = response.charset or "utf-8"
                try:
                    html = b"".join(chunks).decode(charset, errors="replace")
                except LookupError:
                    html = b"".join(chunks).decode("utf-8", errors="replace")
                title, text = extract_readable_text(html, max_chars=max_chars)
                if not text:
                    raise ParseException("网页中没有可总结的正文")
                return WebPageContent(url=current, title=title, text=text)
    raise ParseException("网页重定向次数过多")


__all__ = [
    "BLOCKED_PLATFORM_HOSTS",
    "WebPageContent",
    "extract_readable_text",
    "fetch_public_page",
    "is_blocked_platform_host",
    "validate_public_url",
]
