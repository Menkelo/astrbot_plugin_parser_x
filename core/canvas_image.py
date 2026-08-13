from __future__ import annotations

from pathlib import Path
from shutil import copyfile

from PIL import Image

COMMENT_CANVAS_BASE_WIDTH = 760
COMMENT_CANVAS_SCALED_WIDTH = 1140


def save_comment_canvas_image(rendered_path: Path, out_path: Path) -> None:
    """Save an AstrBot full-page screenshot cropped to the comment element width."""
    rendered_path = Path(rendered_path)
    out_path = Path(out_path)
    same_path = rendered_path.resolve() == out_path.resolve()

    with Image.open(rendered_path) as image:
        target_width = (
            COMMENT_CANVAS_SCALED_WIDTH
            if image.width >= 1000
            else COMMENT_CANVAS_BASE_WIDTH
        )
        if image.width <= target_width:
            cropped = None
            image_format = image.format or "JPEG"
        else:
            cropped = image.crop((0, 0, target_width, image.height))
            cropped.load()
            image_format = image.format or "JPEG"

    if cropped is None:
        if not same_path:
            copyfile(rendered_path, out_path)
        return

    save_kwargs = {}
    if image_format.upper() in {"JPEG", "JPG"}:
        save_kwargs = {"quality": 84, "optimize": True}
    try:
        cropped.save(out_path, format=image_format, **save_kwargs)
    finally:
        cropped.close()


__all__ = [
    "COMMENT_CANVAS_BASE_WIDTH",
    "COMMENT_CANVAS_SCALED_WIDTH",
    "save_comment_canvas_image",
]
