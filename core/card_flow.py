"""全平台通用的「小黑盒式」正文卡编排。

所有平台的图文帖最终都要长成同一种卡片：顶部头图、头像署名行、
正文与图片按原始顺序交替排列。这个小模块把小黑盒那套判定逻辑抽出来，
各平台解析器只要调 :func:`build_card_extra` 就能得到一致的 ``extra``。
"""

from __future__ import annotations

from typing import Any, Iterable

from .topic_text import strip_topic_tags

# 正文流的一个片段：(kind, value)，kind 取 "text" 或 "image"
Flow = list[tuple[str, str]]


def flow_is_interleaved(flow: Flow) -> bool:
    """图片是否被文字夹在中间，而不是连续堆在末尾。

    交错帖每张图都跟着一句说明，拆开发就失去上下文；连续堆叠的图属于
    「配图」，可以单独发。判定看有没有「图片后面还有文字」。
    """
    seen_image = False
    for kind, _value in flow:
        if kind == "image":
            seen_image = True
        elif seen_image:
            return True
    return False


def flow_to_blocks(flow: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """把 ``(kind, value)`` 序列转成渲染层认识的 block 列表。"""
    blocks: list[dict[str, str]] = []
    for kind, value in flow:
        if kind == "image":
            blocks.append({"type": "image", "url": value})
        elif value:
            blocks.append({"type": "text", "text": value})
    return blocks

def _image_identity(url: str) -> str:
    """图片去重用的身份串：取路径最后一段并去掉查询串。

    同一条帖子在缩略图和原图之间只是域名/参数不同（``/thumb.png?imageMogr2/...``
    与原图同名），按文件名比对即可认出是同一张。
    """
    if not url:
        return ""
    path = str(url).split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    # 只要文件名会误判（小红书每条帖子都把图放在不同目录、文件名都叫 thumb.png），
    # 所以带上父目录一起比对。
    return "/".join(parts[-2:]).strip()



def build_card_extra(
    flow: Flow,
    *,
    cover: str | None = None,
    is_video: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按小黑盒规则编排正文卡，返回可直接并入 ``result.extra`` 的字段。

    规则（与小黑盒解析器逐条对齐）：

    1. 有封面就用封面当头图；没有封面且正文只有一张图时，把那张图提到头图，
       并从正文流里摘掉，避免同一张图出现两次。
    2. 两张及以上的图：图文交错则留在卡片里；连续堆叠则把图片从卡片摘掉、
       改为单独发送（``detach``），卡片只留文字。
    3. 卡片不再重复发送同一批图（``keep_single_image_after_card=False``）。
    """
    merged: dict[str, Any] = dict(extra or {})
    merged["render_text_card"] = True
    merged["keep_single_image_after_card"] = False
    # 正文里的话题标签（#话题# / #话题[话题]#）统一去掉，各平台一致。
    flow = [
        (kind, strip_topic_tags(value) if kind == "text" else value)
        for kind, value in flow
    ]
    blocks = flow_to_blocks(flow)
    images = [value for kind, value in flow if kind == "image"]
    hero = cover or None

    if hero is None and len(images) == 1:
        # 单图帖：这张图就是头图
        hero = images[0]
        blocks = [block for block in blocks if block.get("type") != "image"]

    if hero:
        merged["text_card_media"] = hero
        # 头图一律按 900x480 裁切（cover）。原来单图/封面用 contain，
        # 不同比例的图会让卡片高度乱跳，现在统一裁切。
        merged["text_card_media_fit"] = "cover"
        # 同一条帖子又出现在正文流里时摘掉，避免头图位置一张、正文里再来一张。
        # 注意实际落盘的文件可能带缩略图后缀，所以按「文件名」而不是整串 URL 比对。
        # 只从正文流里摘，不动 `images` —— 后面的「多图堆叠/图文交错」判断
        # 仍然按帖子原本的图片数量来定，否则会误判成单图帖。
        hero_key = _image_identity(hero)
        if hero_key:
            blocks = [
                block
                for block in blocks
                if not (
                    block.get("type") == "image"
                    and _image_identity(str(block.get("url") or "")) == hero_key
                )
            ]

    # 图文交错（一句话一张图）时图片是正文的一部分，不能抽走，
    # 但同样用第一张裁一个头图放在最上方，和单图/多图帖保持统一观感。
    if (
        len(images) >= 2
        and flow_is_interleaved(flow)
        and not merged.get("text_card_media")
    ):
        merged["text_card_media"] = images[0]
        merged["text_card_media_fit"] = "cover"

    if len(images) >= 2 and not flow_is_interleaved(flow):
        # 多图堆叠：图片全部单独发，卡片用第一张图裁成头图。
        # 这样群里看到的是「900x480 的裁切头图 + 全部原图」，既统一了版面，
        # 又不丢任何一张图。
        if not merged.get("text_card_media"):
            merged["text_card_media"] = images[0]
            merged["text_card_media_fit"] = "cover"
        blocks = [block for block in blocks if block.get("type") != "image"]
        merged["text_card_detach_images"] = True

    if blocks:
        merged["text_card_flow"] = blocks
    else:
        merged.pop("text_card_flow", None)

    # 正文卡吸干非视频媒体（图片/动图）：
    # - 图片全在卡片正文流里时，不再单独发，避免同一张图出现两次；
    # - 多图堆叠标了 detach，图片要单独发，此时不能吸干，
    #   否则投递计划会把它们过滤掉，结果一张图都发不出来。
    # 视频不受影响，始终直发。
    merged["delivery_text_card_consume_non_video"] = not merged.get(
        "text_card_detach_images"
    )
    # 图文帖统一把正文卡并进「卡片 + 原图」那条合并转发；
    # 之前只有 B站图文动态会打这个标记，其它平台就退化成「卡片一条、图一条」。
    if images and not is_video:
        merged["image_post_card_in_forward"] = True

    return merged


def _format_card_date(timestamp: Any) -> str:
    """把秒级/毫秒级时间戳或 datetime 统一成 ``MM-DD``。"""

    import datetime as _dt

    value = timestamp
    if isinstance(value, (_dt.datetime, _dt.date)):
        moment = value if isinstance(value, _dt.datetime) else _dt.datetime.combine(
            value, _dt.time.min
        )
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        # 空值/0/负数都不是有效时间，直接当作「没有时间」；
        # 例如 0 会被当成 1970-01-01，署名行就会出现莫名其妙的 "01-01"。
        if number <= 0:
            return ""
        # 毫秒时间戳降到秒
        if number > 1e11:
            number /= 1000.0
        try:
            moment = _dt.datetime.fromtimestamp(number)
        except (OverflowError, OSError, ValueError):
            return ""
    return moment.strftime("%m-%d")


def apply_author_block(
    extra: dict[str, Any] | None,
    *,
    avatar: str | None = None,
    timestamp: Any = None,
    location: str | None = None,
) -> dict[str, Any]:
    """补齐卡片署名行的头像与发布时间。

    全平台共用：有头像就用真实头像，没有就由渲染层退回首字母占位；
    署名行只显示发布日期（``10-18``）。

    ``location`` 参数保留但**不再渲染**：各平台作品接口的属地数据质量参差
    （抖音/快手/小红书/B站压根不下发，只有微博和小黑盒有），为了卡片观感
    统一，署名行一律只显示日期。
    """

    del location  # 属地已统一从卡片中移除，仅为兼容既有调用点保留形参
    merged: dict[str, Any] = dict(extra or {})
    avatar_text = str(avatar or "").strip()
    # 解析器可能已经把头像转成了 data URI（B 站这样做，避免渲染时还要联网），
    # 这种情况优先保留，不能被原始 URL 覆盖掉。
    existing = str(merged.get("text_card_avatar") or "").strip()
    if existing:
        pass
    elif avatar_text:
        merged["text_card_avatar"] = avatar_text
    published = _format_card_date(timestamp)
    if published:
        merged["text_card_meta"] = published
    return merged


def build_cover_only_card(
    *,
    cover: str | None,
    is_video: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """没有正文图文顺序信息时的降级编排：只用封面当头图。"""
    return build_card_extra(
        [],
        cover=cover,
        is_video=is_video,
        extra=extra,
    )
