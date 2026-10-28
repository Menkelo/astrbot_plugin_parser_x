from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Any

from astrbot.api import logger


BILIBILI_EMOTE_URL = "https://api.bilibili.com/x/emote/package"
BILIBILI_EMOTE_PANEL_URL = "https://api.bilibili.com/x/emote/user/panel/web"
DOUYIN_EMOTE_URL = "https://www.douyin.com/aweme/v1/web/emoji/list"
XIAOHONGSHU_EMOTE_URL = "https://edith.xiaohongshu.com/api/im/redmoji/detail"
XIAOHONGSHU_EMOJI_CONFIG_URL = "https://edith.xiaohongshu.com/api/im/web/emoji/config"

_SQUARE_TOKEN_RE = re.compile(r"\[[^\[\]\n]{1,64}\]")
_SQUARE_TOKEN_PLATFORMS = {
    "bilibili",
    "douyin",
    "weibo",
    "xiaohongshu",
    # 小黑盒正文里的表情是 [cube_鳄鱼] 这种方括号 token，目录由解析器随结果带上
    "xiaoheihe",
    "tieba",
    "coolapk",
    # NGA 的表情是 [s:ac:汗] 这种方括号 token，历史实现直接把它们从正文里删掉了
    "nga",
}

# The official catalog APIs are the primary source. These small fallbacks keep the
# most common expressions usable during a transient API failure.

_BILIBILI_FALLBACK = {
    "[doge]": (
        "https://i0.hdslb.com/bfs/emote/3087d273a78ccaff4bb1e9972e2ba2a7583c9f11.png"
    ),
    "[笑哭]": (
        "https://i0.hdslb.com/bfs/emote/c3043ba94babf824dea03ce500d0e73763bf4f40.png"
    ),
    "[吃瓜]": (
        "https://i0.hdslb.com/bfs/emote/4191ce3c44c2b3df8fd97c33f85d3ab15f4f3c84.png"
    ),
    "[滑稽]": (
        "https://i0.hdslb.com/bfs/emote/d15121545a99ac46774f1f4465b895fe2d1411c3.png"
    ),
    "[点赞]": (
        "https://i0.hdslb.com/bfs/emote/1a67265993913f4c35d15a6028a30724e83e7d35.png"
    ),
}


_XIAOHONGSHU_FALLBACK = {
    "[微笑R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/9366d16631e3e208689cbc95eefb7cfb0901001e.png"
    ),
    "[偷笑R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/d1a34cf8aeac526d36890d3e8f727192a6808ecf.png"
    ),
    "[大笑R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/aed28089f6578522cd490f636955efe6dd27da38.png"
    ),
    "[笑哭R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/ca75b2fc85b0a3e171fe5df1cbf90efdcd3ba571.png"
    ),
    "[害羞R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/219fe9d7e40b14dd7a6712203143bb1f9972bc5c.png"
    ),
    "[惊恐R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/114d21cd3f1b4a1591cc997ddd5976bb0cec8f4c.png"
    ),
    "[生气R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/91515ae9718d8cce4f8de909683011b538d35327.png"
    ),
    "[哭惹R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/14b005f7afd5f7c88620478b610bf1de90c4ceab.png"
    ),
    "[斜眼R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/6062be312a922da7998f99fb773e06cea0a640df.png"
    ),
    "[鄙视R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/0dbbe487e5157d9fb720df7e59fe45a7927af647.png"
    ),
    "[可怜R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/36338a7a39e27341b34e845e28561378e9ad1ede.png"
    ),
    "[抓狂R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/13619bff18deffe1d2dcc4be0a6ba7ee0394926b.png"
    ),
    "[捂脸R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/83278234fdeb5c36682334f6eb756d243ee62201.png"
    ),
    "[汗颜R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/87e23e577662f3268362518f7f4e90e30b4ea284.png"
    ),
    "[流汗R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/4fc14b31e947deec15d0a1b3f96ae57214ab2bb2.png"
    ),
    "[吃瓜R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/a38d15b09910f65756d521f1f46031c44694214a.png"
    ),
    "[吐舌头H]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/e4533cbaa5829c6ffd92992414290987e39ba6be.png"
    ),
    "[蹲后续H]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/a633dcf8d48c500ae11532d0583c529b89286c66.webp"
    ),
    "[暗中观察R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/0a9cd643452c7b717b9735a23c550295baa69f02.png"
    ),
    "[得意R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/b02bf85f97acbd6be1749148e163b36920655f92.png"
    ),
    "[失望R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/b862c8f94da375f55805a97c152efeeb5099c149.png"
    ),
    "[皱眉R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/fd82d69014a4a50397e20fc6b23ae8dba1c74998.png"
    ),
    "[叹气R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/5ce63c6024defb2f6334aa153fd0fd238a683779.png"
    ),
    "[睡觉R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/d98472a962e744dd238f2b4f5dba2665dcb8360b.png"
    ),
    "[再见R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/c34602650951342f09ca6e00d6f4c4ac57208a07.png"
    ),
    "[抠鼻R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/5fd4922d00a004260912247dad6ca7149d8a1f75.png"
    ),
    "[鼓掌R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/59bbbe6fc2879f6ef42e63b3264096a9f4d403c7.png"
    ),
    "[拥抱R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/efc3b7a9e6df5d2be0233e203adf0d1110623441.png"
    ),
    "[合十R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/fbdbb2547a281e18ee9759e3d658d417871996c0.png"
    ),
    "[点赞R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/391438d25580a034707791b5f165c27f8899025a.png"
    ),
    "[赞R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/1b81c5ec3f7006f6b8baf7c006773f5f9d1ab6d7.png"
    ),
    "[爱心R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/fc7cec55e0e1a0ffd8668d89ea2921c23c63539e.png"
    ),
    "[红色心形R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/d6125900d5de3969a1bb075e23d361c4bd78b0eb.png"
    ),
    "[玫瑰R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/abc0a1cd8434c5348e89e887cf8a4f93f352558c.png"
    ),
    "[火R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/51f1d8e7c5b4182c05510f3aeadecee19e968b42.png"
    ),
    "[烟花R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/64071df3b7c40545149a1d26fcfdf0e704c96c2c.png"
    ),
    "[红薯R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/bfb8a6309b8b42af2cf7c8ce20d1d4fb9a64b512.png"
    ),
    "[红书R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/182d040c46942e0ba1c8eeb66bf7047dad751e72.png"
    ),
    "[doge]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/b7c0498189d449e8f22946be494d6bad48eda5ab.png"
    ),
    "[哇R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/e0771182c12362d41f70356f714d84dccc4d07bc.png"
    ),
    "[喝奶茶R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/364ad5d3e0d5c3b1aa101c9243f488be97d9e8d7.png"
    ),
    "[吧唧R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/238271771c806047fc928b6ba49a6d8e7a741e5e.png"
    ),
    "[派对R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/7a6287c7f65fabdc15fa8f06b2696cccc21e86f2.png"
    ),
    "[庆祝R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/51eab29d66493ab028e9a446c6c10fa606e1e412.png"
    ),
    "[飞吻R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/81cedd016ad9d8bef38b2cd0c1e725454df53598.png"
    ),
    "[石化R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/a61db6b1917b6c5c1e8f30bbeea9118a7bdbbe74.png"
    ),
    "[打卡R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/89214fad0c95300ab58a96037fddafa0415d387e.png"
    ),
    "[种草R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/035c8044c53dbf7df2cf28d6ec35eb325567121b.png"
    ),
    "[私信R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/2062069d03c2927cc823ad0f65c4db645e968058.png"
    ),
    "[萌萌哒R]": (
        "https://picasso-static.xiaohongshu.com/fe-platform/c255f0ae809f8045561a80737b6aec25139f7607.png"
    ),
}

def _load_bundled_map(module_path: str, filename: str) -> dict[str, str]:
    """读取随插件分发的静态表情目录；缺失或损坏时退回空表，不影响解析。"""
    try:
        path = Path(module_path).parent / filename
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug(f"内置表情目录 {filename} 读取失败", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(token): str(url)
        for token, url in data.items()
        if str(token) and str(url).startswith(("http://", "https://"))
    }


def clean_emote_token(platform_key: str, token: str) -> str:
    value = str(token or "").strip()
    if platform_key in _SQUARE_TOKEN_PLATFORMS:
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        return value.strip()
    return value


def fallback_emote_map(platform_key: str) -> dict[str, str]:
    if platform_key == "bilibili":
        return dict(_BILIBILI_FALLBACK)
    if platform_key == "nga":
        return _load_bundled_map(__file__, "parsers/nga_emotes.json")
    if platform_key == "xiaohongshu":
        from .xiaohongshu_emote_data import XIAOHONGSHU_EMOTE_DATA_URI

        return {**_XIAOHONGSHU_FALLBACK, **XIAOHONGSHU_EMOTE_DATA_URI}
    return {}


def _register(
    output: dict[str, str],
    platform_key: str,
    name: object,
    url: object,
) -> None:
    name_text = str(name or "").strip()
    url_text = str(url or "").strip()
    # 只接受 http(s)：线上接口返回的载荷不得注入 data:/javascript: 等内联来源。
    # 内嵌底图走 fallback_emote_map，不经过这里。
    if not name_text or not url_text.startswith(("http://", "https://")):
        return
    keys = {name_text, clean_emote_token(platform_key, name_text)}
    for key in keys:
        if not key:
            continue
        output[key] = url_text


def build_bilibili_emote_map(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}

    output: dict[str, str] = {}
    for package in data.get("packages") or []:
        if not isinstance(package, dict):
            continue
        for item in package.get("emote") or []:
            if not isinstance(item, dict):
                continue
            _register(
                output,
                "bilibili",
                item.get("text") or item.get("name"),
                item.get("url") or item.get("gif_url"),
            )
    return output


def build_xiaohongshu_emote_map(payload: object) -> dict[str, str]:
    """解析小红书 redmoji 目录。

    线上接口结构为 data.emoji.tabs[].collection[].emoji[]，每一项含 image 与
    image_name（形如 "[微笑R]"）。不同入口/版本的字段名与层级略有差异，这里
    对 data 包裹、collection/collections、emoji/emojis 及各字段别名一并兼容，
    只保留能解析出 http(s) 图片地址的条目（由 _register 兜底）。
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload

    emoji = data.get("emoji")
    if not isinstance(emoji, dict):
        emoji = data
    tabs = emoji.get("tabs")
    if not isinstance(tabs, list):
        tabs = data.get("tabs")

    output: dict[str, str] = {}
    for tab in tabs or []:
        if not isinstance(tab, dict):
            continue
        collections = tab.get("collection") or tab.get("collections") or []
        if isinstance(collections, dict):
            collections = [collections]
        for collection in collections:
            if not isinstance(collection, dict):
                continue
            items = collection.get("emoji") or collection.get("emojis") or []
            if isinstance(items, dict):
                items = [items]
            for item in items:
                if not isinstance(item, dict):
                    continue
                _register(
                    output,
                    "xiaohongshu",
                    item.get("image_name")
                    or item.get("imageName")
                    or item.get("display_name")
                    or item.get("name")
                    or item.get("text"),
                    item.get("image")
                    or item.get("url")
                    or item.get("src")
                    or item.get("image_url"),
                )
    return output


def build_xiaohongshu_emote_config_map(payload: object) -> dict[str, str]:
    """解析 ``/api/im/web/emoji/config`` 的静态表情目录。

    结构与 redmoji/detail 不同：表情在 ``data.tabs[].collections[].tuples[]``，
    每项含 ``image_name``（形如 "[嗨R]"）与 ``image_url``。部分条目
    ``image_url`` 为空（纯动画表情，只有 ``image_animated_url``），跳过即可，
    这些由内嵌底图兜底。
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload

    output: dict[str, str] = {}
    for tab in data.get("tabs") or []:
        if not isinstance(tab, dict):
            continue
        collections = tab.get("collections") or tab.get("collection") or []
        if isinstance(collections, dict):
            collections = [collections]
        for collection in collections:
            if not isinstance(collection, dict):
                continue
            tuples = collection.get("tuples") or []
            if isinstance(tuples, dict):
                tuples = [tuples]
            for item in tuples:
                if not isinstance(item, dict):
                    continue
                _register(
                    output,
                    "xiaohongshu",
                    item.get("image_name") or item.get("text"),
                    item.get("image_url") or item.get("image"),
                )
    return output


def build_douyin_emote_map(payload: object) -> dict[str, str]:
    """解析抖音 ``/aweme/v1/web/emoji/list`` 的官方表情目录。

    结构为 ``emoji_list[].{display_name, emoji_url.url_list[]}``。抖音只给带签名
    参数的临时地址，所以这里只取目录做「token 是否被平台认识」的判定；真正出图
    由 ``EmoteResolver`` 在渲染期现取现用（见 core/emote_sources.py）。
    """
    if not isinstance(payload, dict):
        return {}
    output: dict[str, str] = {}
    for item in payload.get("emoji_list") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or item.get("emoji_name") or "").strip()
        emoji_url = item.get("emoji_url")
        urls = []
        if isinstance(emoji_url, dict):
            urls = emoji_url.get("url_list") or []
        elif isinstance(emoji_url, str):
            urls = [emoji_url]
        url = next((str(u) for u in urls if str(u).startswith("https://")), "")
        _register(output, "douyin", name, url)
    return output


def resolve_emote_url(
    platform_key: str,
    token: str,
    emotes: Mapping[str, str] | None,
) -> str:
    catalog = emotes or {}
    raw = str(token or "").strip()
    clean = clean_emote_token(platform_key, raw)
    for key in (raw, clean):
        value = str(catalog.get(key) or "").strip()
        if value.startswith(("http://", "https://")) or re.fullmatch(
            r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}", value
        ):
            return value
    return ""


def iter_emote_matches(
    text: str,
    platform_key: str,
    emotes: Mapping[str, str] | None = None,
) -> Iterator[tuple[int, int, str, str]]:
    pattern = _SQUARE_TOKEN_RE if platform_key in _SQUARE_TOKEN_PLATFORMS else None
    if pattern is None:
        return
    for matched in pattern.finditer(text or ""):
        token = matched.group(0)
        yield (
            matched.start(),
            matched.end(),
            token,
            resolve_emote_url(
                platform_key,
                token,
                emotes,
            ),
        )


def contains_platform_emotes(text: str, platform_key: str) -> bool:
    return any(iter_emote_matches(text, platform_key))


def select_text_emotes(
    text: str,
    platform_key: str,
    catalog: Mapping[str, str] | None,
) -> dict[str, str]:
    selected: dict[str, str] = {}
    for _, _, token, url in iter_emote_matches(text, platform_key, catalog):
        if url:
            selected[token] = url
            selected[clean_emote_token(platform_key, token)] = url
    return selected


async def load_platform_emotes(
    parser: Any,
    platform_key: str,
) -> dict[str, str]:
    cache_attr = f"_parser_x_{platform_key}_emote_map"
    deadline_attr = f"{cache_attr}_deadline"
    now = time.monotonic()
    cached = getattr(parser, cache_attr, None)
    deadline = float(getattr(parser, deadline_attr, 0.0) or 0.0)
    if isinstance(cached, dict) and cached and now < deadline:
        return cached

    output = fallback_emote_map(platform_key)
    http_get = getattr(parser, "http_get", None)
    # 抖音/小黑盒的目录由它们自己的解析流程维护，不再重复拉取。
    if (
        platform_key not in {"bilibili", "xiaohongshu", "douyin"}
        or not callable(http_get)
    ):
        setattr(parser, cache_attr, output)
        setattr(parser, deadline_attr, now + 10 * 60)
        return output

    try:
        request_headers = dict(getattr(parser, "headers", None) or {})
        if platform_key == "douyin":
            # 目录接口必须带 a_bogus 签名，直接复用评论链路里已经验证过的签名器，
            # 否则只会拿到空响应，正文表情就会以文字形式漏出来。
            from .parsers.douyin.a_bogus import SIGN_ARGS_EMOJI, generate_a_bogus

            douyin_cookie = str(getattr(parser, "douyin_ck", "") or "").strip()
            if douyin_cookie:
                request_headers["Cookie"] = douyin_cookie
            sign_params = {
                "device_platform": "webapp",
                "aid": 6383,
                "channel": "channel_pc_web",
                "need_all": "true",
                "update_version_code": 170400,
            }
            query = urllib.parse.urlencode(sign_params)
            response = await http_get(
                DOUYIN_EMOTE_URL,
                params={
                    **sign_params,
                    "a_bogus": generate_a_bogus(
                        query,
                        request_headers.get("User-Agent", ""),
                        sign_args=SIGN_ARGS_EMOJI,
                    ),
                },
                headers=request_headers,
                timeout=8,
                retries=1,
            )
            parsed = build_douyin_emote_map(response.json())
        elif platform_key == "xiaohongshu":
            # emoji/config 需要登录态，redmoji/detail 不需要；统一带上评论 cookie。
            xhs_cookie = str(getattr(parser, "comment_cookie", "") or "").strip()
            if xhs_cookie:
                request_headers["Cookie"] = xhs_cookie
            response = await http_get(
                XIAOHONGSHU_EMOTE_URL,
                headers=request_headers,
                timeout=8,
                retries=1,
            )
            parsed = build_xiaohongshu_emote_map(response.json())
            # emoji/config 提供 redmoji/detail 之外的静态表情（含新面板），
            # 拿不到就退回内嵌底图，不影响主流程。
            try:
                config_response = await http_get(
                    XIAOHONGSHU_EMOJI_CONFIG_URL,
                    headers=request_headers,
                    timeout=8,
                    retries=1,
                )
                extra = build_xiaohongshu_emote_config_map(config_response.json())
                for token, url in extra.items():
                    parsed.setdefault(token, url)
                logger.debug(f"[xiaohongshu] emoji/config 补充 {len(extra)} 个表情")
            except Exception as exc:
                logger.debug(f"[xiaohongshu] emoji/config 读取失败，忽略: {exc}")
        else:
            bili_cookie = str(getattr(parser, "bili_ck", "") or "").strip()
            if bili_cookie:
                request_headers["Cookie"] = bili_cookie
            response = await http_get(
                BILIBILI_EMOTE_URL,
                params={"business": "reply", "ids": "1"},
                headers=request_headers,
                timeout=8,
                retries=1,
            )
            parsed = build_bilibili_emote_map(response.json())
            if bili_cookie:
                try:
                    panel_response = await http_get(
                        BILIBILI_EMOTE_PANEL_URL,
                        params={"business": "reply"},
                        headers=request_headers,
                        timeout=8,
                        retries=1,
                    )
                    panel_payload = panel_response.json()
                    parsed.update(build_bilibili_emote_map(panel_payload))
                    packages = (
                        (panel_payload.get("data") or {}).get("packages") or []
                        if isinstance(panel_payload, dict)
                        else []
                    )
                    package_ids = [
                        str(package.get("id"))
                        for package in packages
                        if isinstance(package, dict)
                        and package.get("id") not in (None, "", 1, "1")
                    ]
                    if package_ids:
                        owned_response = await http_get(
                            BILIBILI_EMOTE_URL,
                            params={
                                "business": "reply",
                                "ids": ",".join(dict.fromkeys(package_ids)),
                            },
                            headers=request_headers,
                            timeout=8,
                            retries=1,
                        )
                        parsed.update(build_bilibili_emote_map(owned_response.json()))
                except Exception as exc:
                    logger.debug(f"[bilibili] 用户表情包读取失败，保留公共表情: {exc}")

        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        output.update(parsed)
        ttl = 6 * 60 * 60 if parsed else 10 * 60
    except Exception as exc:
        logger.debug(f"[{platform_key}] 表情目录读取失败，使用内置兜底: {exc}")
        ttl = 10 * 60

    setattr(parser, cache_attr, output)
    setattr(parser, deadline_attr, now + ttl)
    return output


async def resolve_platform_emotes(
    parser: Any,
    platform_key: str,
    emotes: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """把目录里的表情补成可在卡片里显示的内联地址。

    目录本身可能只给出官方地址；这里交给运行时解析器按需下载并缓存，
    下载失败的表情会回落到 ``[表情名]`` 文本，不会影响卡片其余内容。
    """
    catalog = dict(emotes or {})
    if not catalog:
        return catalog
    resolver = getattr(parser, "emote_resolver", None)
    if resolver is None:
        return catalog
    tokens = set()
    for token, value in catalog.items():
        if not value:
            tokens.add(token)
    if not tokens:
        return catalog
    try:
        resolved = await resolver.resolve(platform_key, tokens)
    except Exception as exc:
        logger.debug(f"[{platform_key}] 表情运行时解析失败: {exc}")
        return catalog
    if not resolved:
        return catalog
    for token, uri in resolved.items():
        catalog[token] = uri
        catalog[clean_emote_token(platform_key, token)] = uri
    return catalog


__all__ = [
    "build_bilibili_emote_map",
    "build_douyin_emote_map",
    "build_xiaohongshu_emote_config_map",
    "build_xiaohongshu_emote_map",
    "clean_emote_token",
    "contains_platform_emotes",
    "fallback_emote_map",
    "iter_emote_matches",
    "load_platform_emotes",
    "resolve_emote_url",
    "resolve_platform_emotes",
    "select_text_emotes",
]
