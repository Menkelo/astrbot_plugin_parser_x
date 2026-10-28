from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Iterable

# Modified Python adaptation of rconsole-plugin/utils/a-bogus.cjs.
# Source: https://github.com/zhiyu1998/rconsole-plugin/blob/master/utils/a-bogus.cjs
# Upstream repository license: Mulan PSL v2. The file also carries this notice:
# All the content in this article is only for learning and communication use, not for any other purpose, strictly prohibited for commercial use and illegal use, otherwise all the consequences are irrelevant to the author!
# 中文说明：上游声明仅供学习交流，禁止商用及非法使用。
# 仓库许可证与该文件额外声明的适用关系尚未澄清；此处保留原文，
# 不据此宣称已取得额外授权，也不以插件 MIT 许可覆盖本模块的上游条款。
# See THIRD_PARTY_NOTICES.md for attribution, unresolved scope, and full license.

_S3 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
_S4 = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
_WINDOW_ENV = "1536|747|1536|834|0|30|0|0|1536|834|1536|864|1525|747|24|24|Win32"

# 抖音网页接口按用途使用不同的签名参数三元组，取自官方 signer：
#   sign_datail -> [0, 1, 14]  评论列表 / 作品详情等
#   sign_reply  -> [0, 1, 8]   评论回复（楼中楼）
# 参数不匹配时服务端不会报错，只返回 200 + 空 body，因此必须区分。
SIGN_ARGS_DETAIL = (0, 1, 14)
SIGN_ARGS_REPLY = (0, 1, 8)
# 表情目录接口与评论详情共用同一组签名参数（实测 200 且返回完整 emoji_list）。
SIGN_ARGS_EMOJI = SIGN_ARGS_DETAIL


def _rc4_encrypt(plaintext: bytes, key: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]

    i = 0
    j = 0
    output = bytearray()
    for value in plaintext:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        output.append(value ^ state[(state[i] + state[j]) % 256])
    return bytes(output)


def _custom_base64(data: bytes, alphabet: str) -> str:
    output = []
    for offset in range(0, len(data), 3):
        chunk = data[offset : offset + 3]
        value = chunk[0] << 16
        if len(chunk) > 1:
            value |= chunk[1] << 8
        if len(chunk) > 2:
            value |= chunk[2]

        output.append(alphabet[(value >> 18) & 63])
        output.append(alphabet[(value >> 12) & 63])
        if len(chunk) > 1:
            output.append(alphabet[(value >> 6) & 63])
        if len(chunk) > 2:
            output.append(alphabet[value & 63])

    output.extend("=" * ((4 - len(output) % 4) % 4))
    return "".join(output)


def _rotate_left(value: int, bits: int) -> int:
    bits %= 32
    return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF


def _sm3_fallback(data: bytes) -> bytes:
    """Small pure-Python SM3 fallback for OpenSSL builds without SM3."""

    registers = [
        0x7380166F,
        0x4914B2B9,
        0x172442D7,
        0xDA8A0600,
        0xA96F30BC,
        0x163138AA,
        0xE38DEE4D,
        0xB0FB0E4E,
    ]
    message = bytearray(data)
    bit_length = len(message) * 8
    message.append(0x80)
    while len(message) % 64 != 56:
        message.append(0)
    message.extend(bit_length.to_bytes(8, "big"))

    for offset in range(0, len(message), 64):
        block = message[offset : offset + 64]
        words = [
            int.from_bytes(block[index : index + 4], "big") for index in range(0, 64, 4)
        ]
        words.extend([0] * 52)
        for index in range(16, 68):
            value = (
                words[index - 16]
                ^ words[index - 9]
                ^ _rotate_left(words[index - 3], 15)
            )
            value ^= _rotate_left(value, 15) ^ _rotate_left(value, 23)
            words[index] = (
                value ^ _rotate_left(words[index - 13], 7) ^ words[index - 6]
            ) & 0xFFFFFFFF
        derived = [words[index] ^ words[index + 4] for index in range(64)]

        a, b, c, d, e, f, g, h = registers
        for index in range(64):
            constant = 0x79CC4519 if index < 16 else 0x7A879D8A
            ss1 = _rotate_left(
                (_rotate_left(a, 12) + e + _rotate_left(constant, index)) & 0xFFFFFFFF,
                7,
            )
            ss2 = ss1 ^ _rotate_left(a, 12)
            if index < 16:
                ff = a ^ b ^ c
                gg = e ^ f ^ g
            else:
                ff = (a & b) | (a & c) | (b & c)
                gg = (e & f) | ((~e) & g)
            tt1 = (ff + d + ss2 + derived[index]) & 0xFFFFFFFF
            tt2 = (gg + h + ss1 + words[index]) & 0xFFFFFFFF
            d = c
            c = _rotate_left(b, 9)
            b = a
            a = tt1
            h = g
            g = _rotate_left(f, 19)
            f = e
            e = tt2 ^ _rotate_left(tt2, 9) ^ _rotate_left(tt2, 17)

        registers = [
            left ^ right for left, right in zip(registers, (a, b, c, d, e, f, g, h))
        ]

    return b"".join(value.to_bytes(4, "big") for value in registers)


def _sm3(data: bytes) -> bytes:
    try:
        return hashlib.new("sm3", data).digest()
    except (TypeError, ValueError):
        return _sm3_fallback(data)


def _double_sm3(data: bytes) -> bytes:
    return _sm3(_sm3(data))


def _random_group(value: float, option: tuple[int, int]) -> bytes:
    number = int(value * 10_000)
    return bytes(
        [
            (number & 255 & 170) | (option[0] & 85),
            (number & 255 & 85) | (option[0] & 170),
            ((number >> 8) & 255 & 170) | (option[1] & 85),
            ((number >> 8) & 255 & 85) | (option[1] & 170),
        ]
    )


def _random_prefix(values: Iterable[float] | None = None) -> bytes:
    source = iter(values) if values is not None else None

    def next_value() -> float:
        if source is None:
            return random.random()
        return next(source)

    return b"".join(
        (
            _random_group(next_value(), (3, 45)),
            _random_group(next_value(), (1, 0)),
            _random_group(next_value(), (1, 5)),
        )
    )


def generate_a_bogus(
    query: str,
    user_agent: str,
    *,
    now_ms: int | None = None,
    random_values: Iterable[float] | None = None,
    sign_args: tuple[int, int, int] = SIGN_ARGS_DETAIL,
) -> str:
    """Generate the web API ``a_bogus`` value used by Douyin comments.

    ``sign_args`` 对应官方 signer 的第三参数：评论列表/详情用
    ``SIGN_ARGS_DETAIL``，评论回复用 ``SIGN_ARGS_REPLY``。用错只会拿到空响应。
    """

    start_time = now_ms if now_ms is not None else int(time.time() * 1000)
    suffix = b"cus"
    params_hash = _double_sm3(query.encode("utf-8") + suffix)
    suffix_hash = _double_sm3(suffix)
    ua_bytes = bytes(ord(char) & 0xFF for char in user_agent)
    ua_cipher = _rc4_encrypt(ua_bytes, bytes((0, 1, 14)))
    ua_hash = _sm3(_custom_base64(ua_cipher, _S3).encode("ascii"))
    end_time = now_ms if now_ms is not None else int(time.time() * 1000)

    values = [0] * 73
    values[8] = 3
    values[10] = end_time
    values[16] = start_time
    values[18] = 44

    values[20] = (start_time >> 24) & 255
    values[21] = (start_time >> 16) & 255
    values[22] = (start_time >> 8) & 255
    values[23] = start_time & 255
    values[24] = int(start_time / 256**4) & 255
    values[25] = int(start_time / 256**5) & 255

    arguments = sign_args
    values[26] = (arguments[0] >> 24) & 255
    values[27] = (arguments[0] >> 16) & 255
    values[28] = (arguments[0] >> 8) & 255
    values[29] = arguments[0] & 255
    values[30] = (arguments[1] // 256) & 255
    values[31] = arguments[1] & 255
    values[32] = (arguments[1] >> 24) & 255
    values[33] = (arguments[1] >> 16) & 255
    values[34] = (arguments[2] >> 24) & 255
    values[35] = (arguments[2] >> 16) & 255
    values[36] = (arguments[2] >> 8) & 255
    values[37] = arguments[2] & 255

    values[38] = params_hash[21]
    values[39] = params_hash[22]
    values[40] = suffix_hash[21]
    values[41] = suffix_hash[22]
    values[42] = ua_hash[23]
    values[43] = ua_hash[24]

    values[44] = (end_time >> 24) & 255
    values[45] = (end_time >> 16) & 255
    values[46] = (end_time >> 8) & 255
    values[47] = end_time & 255
    values[48] = values[8]
    values[49] = int(end_time / 256**4) & 255
    values[50] = int(end_time / 256**5) & 255

    page_id = 6241
    values[52] = (page_id >> 24) & 255
    values[53] = (page_id >> 16) & 255
    values[54] = (page_id >> 8) & 255
    values[55] = page_id & 255

    aid = 6383
    values[57] = aid & 255
    values[58] = (aid >> 8) & 255
    values[59] = (aid >> 16) & 255
    values[60] = (aid >> 24) & 255

    environment = _WINDOW_ENV.encode("ascii")
    values[65] = len(environment) & 255
    values[66] = (len(environment) >> 8) & 255
    values[70] = 0
    values[71] = 0

    checksum_indexes = (
        18,
        20,
        26,
        30,
        38,
        40,
        42,
        21,
        27,
        31,
        35,
        39,
        41,
        43,
        22,
        28,
        32,
        36,
        23,
        29,
        33,
        37,
        44,
        45,
        46,
        47,
        48,
        49,
        50,
        24,
        25,
        52,
        53,
        54,
        55,
        57,
        58,
        59,
        60,
        65,
        66,
        70,
        71,
    )
    checksum = 0
    for index in checksum_indexes:
        checksum ^= values[index]

    output_indexes = (
        18,
        20,
        52,
        26,
        30,
        34,
        58,
        38,
        40,
        53,
        42,
        21,
        27,
        54,
        55,
        31,
        35,
        57,
        39,
        41,
        43,
        22,
        28,
        32,
        60,
        36,
        23,
        29,
        33,
        37,
        44,
        45,
        59,
        46,
        47,
        48,
        49,
        50,
        24,
        25,
        65,
        66,
        70,
        71,
    )
    body = bytes(values[index] for index in output_indexes)
    body += environment + bytes((checksum,))
    encrypted = _rc4_encrypt(body, b"y")
    return _custom_base64(_random_prefix(random_values) + encrypted, _S4)


__all__ = ["generate_a_bogus", "SIGN_ARGS_DETAIL", "SIGN_ARGS_EMOJI", "SIGN_ARGS_REPLY"]
