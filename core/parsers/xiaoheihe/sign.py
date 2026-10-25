"""小黑盒 web 接口 hkey / nonce 签名。

逆向自 https://www.xiaoheihe.cn 前端 ``hkey&nonce.js``，与社区实现
(nonebot-plugin-parser-lite / 焦化葱葱) 的等价变换保持一致。

算法要点：
- ``nonce = md5(str(ts)).hexdigest().upper()``
- ``hkey`` 由路径、``ts + 1``、``nonce`` 三者交织后 MD5 得到
"""

from __future__ import annotations

import hashlib
import itertools
from typing import Final

# 混淆字符表
_TABLE: Final[str] = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _vm(e: int) -> int:
    return ((e << 1) & 0xFF) ^ 27 if (e & 0x80) else ((e << 1) & 0xFF)


def _qm(e: int) -> int:
    return _vm(e) ^ e


def _mm(e: int) -> int:
    return _qm(_vm(e))


def _ym(e: int) -> int:
    return _mm(_qm(_vm(e)))


def _gm(e: int) -> int:
    return _ym(e) ^ _mm(e) ^ _qm(e)


def _km(e: list[int]) -> list[int]:
    t0 = _gm(e[0]) ^ _ym(e[1]) ^ _mm(e[2]) ^ _qm(e[3])
    t1 = _qm(e[0]) ^ _gm(e[1]) ^ _ym(e[2]) ^ _mm(e[3])
    t2 = _mm(e[0]) ^ _qm(e[1]) ^ _gm(e[2]) ^ _ym(e[3])
    t3 = _ym(e[0]) ^ _mm(e[1]) ^ _qm(e[2]) ^ _gm(e[3])
    e[0], e[1], e[2], e[3] = t0, t1, t2, t3
    return e


def _av(e: str, t: str, n: int) -> str:
    """等价 JS av(e, t, n)：按字符码取模映射。"""
    i = t[:n]
    if not i:
        return ""
    return "".join(i[ord(ch) % len(i)] for ch in e)


def _sv(e: str, t: str) -> str:
    if not t:
        return ""
    return "".join(t[ord(ch) % len(t)] for ch in e)


def _interleave(arr: list[str]) -> str:
    """把多个字符串按位交织，等价 JS 里那段 Math.max 循环。"""
    if not arr:
        return ""
    max_len = max(len(s) for s in arr)
    return "".join(
        s[i] for i, s in itertools.product(range(max_len), arr) if i < len(s)
    )


def get_nonce(time: int) -> str:
    return hashlib.md5(str(time).encode("utf-8")).hexdigest().upper()


def get_hkey(path: str, time: int) -> str:
    """按前端逻辑生成接口所需 hkey。"""
    e = path
    t = time + 1
    n = get_nonce(time)

    parts = [seg for seg in e.split("/") if seg]
    e_norm = "/" + "/".join(parts) + "/"

    r = _TABLE
    i_str = _interleave([_av(str(t), r, -2), _sv(e_norm, r), _sv(n, r)])[:20]

    o = hashlib.md5(i_str.encode("utf-8")).hexdigest()
    mixed = _km([ord(ch) for ch in o[-6:]])
    a = f"{sum(mixed) % 100:02d}"
    s = _av(o[:5], r, -4)

    return f"{s}{a}"


__all__ = ["get_hkey", "get_nonce"]
