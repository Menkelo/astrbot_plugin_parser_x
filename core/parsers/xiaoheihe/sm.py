"""小黑盒 web 端 SM 风控指纹载荷生成。

小黑盒 ``api.xiaoheihe.cn`` 对未携带指纹的请求会返回
``{"status": "show_captcha"}``。此处按前端 SDK 的加密规范生成
``/deviceprofile/v4`` 所需载荷，换取 deviceId 后再请求业务接口。

加密规范（与前端保持一致）：
1. 浏览器环境字段按规则表用 3DES-ECB 加密并 base64 编码，key 换成混淆名；
2. 结果序列化后 gzip 压缩；
3. 再用 AES-CBC(key=priId) 加密并转 hex。

来源：nonebot-plugin-parser-lite 与 skyland-auto-sign 的社区逆向实现。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import random
import time
import uuid
from typing import Any, Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

SM_HOST: Final[str] = "fp-it.portal101.cn"
SM_APP_ID: Final[str] = "heybox_website"
SM_ORGANIZATION: Final[str] = "0yD85BjYvGFAvHaSQ1mc"
SM_PUBLIC_KEY: Final[str] = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCXj9exmI4nQjmT52iwr+yf7hAQ06bfSZHTAHUfRBYi"
    "agCf/whhd8es0R79wBigpiHLd28TKA8b8mGR8OiiI1hV+qfynCWihvp3mdj8MiiH6SU3lhro2hkfYzIm"
    "ZB0RmWr2zE4Xt1+A6Oyp6bf+W7JSxYUXHw3nNv7Td4jw4jEFKQIDAQAB"
)

# 字段 -> (3DES key, 混淆后字段名)；仅列出需要加密的字段
_DES_RULES: Final[dict[str, tuple[str, str]]] = {
    "appId": ("uy7mzc4h", "xx"),
    "canvas": ("snrn887t", "yk"),
    "clientSize": ("cpmjjgsu", "zx"),
    "organization": ("78moqjfc", "dp"),
    "os": ("je6vk6t4", "pj"),
    "platform": ("pakxhcd2", "gm"),
    "plugins": ("v51m3pzl", "kq"),
    "pmf": ("2mdeslu3", "vw"),
    "referer": ("y7bmrjlc", "ab"),
    "res": ("whxqm2a7", "hf"),
    "rtype": ("x8o2h2bl", "lo"),
    "sdkver": ("9q3dcxp2", "sc"),
    "status": ("2jbrxxw4", "an"),
    "subVersion": ("eo3i2puh", "ns"),
    "svm": ("fzj3kaeh", "qr"),
    "time": ("q2t3odsk", "nb"),
    "timezone": ("1uv05lj5", "as"),
    "tn": ("x9nzj1bp", "py"),
    "trees": ("acfs0xo4", "pi"),
    "ua": ("k92crp1t", "bj"),
    "url": ("y95hjkoo", "cf"),
    "vpw": ("r9924ab5", "ca"),
}

# 不加密、仅改名的字段
_PLAIN_RULES: Final[dict[str, str]] = {
    "box": "jf",
    "version": "version",
    "protocol": "protocol",
}

_BROWSER_ENV: Final[dict[str, Any]] = {
    "plugins": (
        "MicrosoftEdgePDFPluginPortableDocumentFormatinternal-pdf-viewer1,"
        "MicrosoftEdgePDFViewermhjfbmdgcfjbbpaeojofohoefgiehjai1"
    ),
    "ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
    ),
    "timezone": -480,
    "platform": "Win32",
    "url": "https://www.xiaoheihe.cn/",
    "referer": "",
    "res": "1920_1080_24_1.25",
    "clientSize": "0_0_1080_1920_1920_1080_1920_1080",
    "status": "0011",
}


def _triple_des():
    """TripleDES 在 cryptography>=43 迁到了 decrepit 命名空间，需兼容。"""
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import (
            TripleDES as _TD,
        )
    except ImportError:  # pragma: no cover - 旧版本 cryptography
        from cryptography.hazmat.primitives.ciphers.algorithms import (
            TripleDES as _TD,
        )
    return _TD


def _des_encrypt(text: str, key: str) -> str:
    """3DES-ECB 零填充加密后 base64。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    data = text.encode("utf-8")
    if pad := (-len(data)) % 8:
        data += b"\x00" * pad
    cipher = Cipher(_triple_des()(key.encode() * 3), modes.ECB())
    enc = cipher.encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _aes_encrypt(raw: bytes, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = b"0102030405060708"
    data = raw + b"\x00"
    if pad := (-len(data)) % 16:
        data += b"\x00" * pad
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return (enc.update(data) + enc.finalize()).hex()


def _encrypt_fields(source: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in source.items():
        if name in _DES_RULES:
            key, alias = _DES_RULES[name]
            out[alias] = _des_encrypt(str(value), key)
        elif name in _PLAIN_RULES:
            out[_PLAIN_RULES[name]] = value
        else:
            out[name] = value
    return out


def _gzip_b64(obj: dict[str, Any]) -> bytes:
    # ensure_ascii=False 与前端 JSON.stringify 一致；不压缩会改变 tn 校验
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, 2, mtime=0))


def _tn(obj: dict[str, Any]) -> str:
    """把全部字段值递归拼接成校验串（数字统一放大 10000 倍）。"""
    parts: list[str] = []
    for key in sorted(obj):
        value = obj[key]
        if isinstance(value, int | float):
            parts.append(str(value * 10000))
        elif isinstance(value, dict):
            parts.append(_tn(value))
        else:
            parts.append(str(value))
    return "".join(parts)


def _smid() -> str:
    t = time.localtime()
    stamp = (
        f"{t.tm_year}{t.tm_mon:02d}{t.tm_mday:02d}"
        f"{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}"
    )
    mid = hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()
    value = f"{stamp}{mid}00"
    smsk = hashlib.md5(f"smsk_web_{value}".encode()).hexdigest()[:14]
    return f"{value}{smsk}0"


def build_payload() -> dict[str, Any]:
    """构造 /deviceprofile/v4 的请求体。"""
    uid = str(uuid.uuid4()).encode("utf-8")
    pri_id = hashlib.md5(uid).hexdigest()[:16]

    pub = serialization.load_der_public_key(base64.b64decode(SM_PUBLIC_KEY))
    ep = base64.b64encode(pub.encrypt(uid, padding.PKCS1v15())).decode()

    now_ms = int(time.time() * 1000)
    browser = dict(_BROWSER_ENV)
    browser.update(
        {
            "canvas": "".join(random.choice("0123456789abcdef") for _ in range(8)),
            "vpw": str(uuid.uuid4()),
            "svm": now_ms,
            "trees": str(uuid.uuid4()),
            "pmf": now_ms,
        }
    )

    target: dict[str, Any] = {
        **browser,
        "protocol": 102,
        "organization": SM_ORGANIZATION,
        "appId": SM_APP_ID,
        "os": "web",
        "version": "3.0.0",
        "sdkver": "3.0.0",
        "box": "",
        "rtype": "all",
        "smid": _smid(),
        "subVersion": "1.0.0",
        "time": 0,
    }
    target["tn"] = hashlib.md5(_tn(target).encode()).hexdigest()

    data = _aes_encrypt(_gzip_b64(_encrypt_fields(target)), pri_id.encode("utf-8"))
    return {
        "appId": SM_APP_ID,
        "compress": 2,
        "data": data,
        "encode": 5,
        "ep": ep,
        "organization": SM_ORGANIZATION,
        "os": "web",
    }


__all__ = ["SM_HOST", "build_payload"]
