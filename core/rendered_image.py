from __future__ import annotations

from pathlib import Path
from shutil import copyfile

from PIL import Image, ImageChops, ImageStat


def _background_color(image: Image.Image) -> tuple[int, int, int]:
    """Estimate the page background from the two bottom corners."""
    sample_width = min(24, image.width)
    sample_height = min(24, image.height)
    sample = Image.new("RGB", (sample_width * 2, sample_height))
    try:
        left = image.crop(
            (0, image.height - sample_height, sample_width, image.height)
        ).convert("RGB")
        right = image.crop(
            (
                image.width - sample_width,
                image.height - sample_height,
                image.width,
                image.height,
            )
        ).convert("RGB")
        try:
            sample.paste(left, (0, 0))
            sample.paste(right, (sample_width, 0))
            return tuple(round(value) for value in ImageStat.Stat(sample).median)
        finally:
            left.close()
            right.close()
    finally:
        sample.close()


def _content_bottom(
    image: Image.Image,
    *,
    background_tolerance: int,
) -> int | None:
    """Return the exclusive bottom edge of pixels differing from the page."""
    background_color = _background_color(image)
    threshold = [0] * (background_tolerance + 1) + [255] * (255 - background_tolerance)
    strip_bottom = image.height

    while strip_bottom > 0:
        strip_top = max(0, strip_bottom - 256)
        strip = image.crop((0, strip_top, image.width, strip_bottom)).convert("RGB")
        background = Image.new("RGB", strip.size, background_color)
        difference = ImageChops.difference(strip, background)
        mask = None
        try:
            for band in difference.split():
                band_mask = band.point(threshold)
                if mask is None:
                    mask = band_mask
                else:
                    combined = ImageChops.lighter(mask, band_mask)
                    mask.close()
                    band_mask.close()
                    mask = combined
            bbox = mask.getbbox() if mask is not None else None
            if bbox is not None:
                return strip_top + bbox[3]
        finally:
            if mask is not None:
                mask.close()
            difference.close()
            background.close()
            strip.close()
        strip_bottom = strip_top

    return None


def save_rendered_image(
    rendered_path: Path,
    out_path: Path,
    *,
    target_width: int | None = None,
    fallback_width: int | None = None,
    bottom_padding: int | None = None,
    background_tolerance: int = 10,
    jpeg_quality: int = 84,
) -> None:
    """Save an html_render result with unused right/bottom canvas removed."""
    rendered_path = Path(rendered_path)
    out_path = Path(out_path)
    same_path = rendered_path.resolve() == out_path.resolve()

    with Image.open(rendered_path) as image:
        image.load()
        original_width = image.width
        original_height = image.height
        image_format = image.format or "PNG"
        crop_width = image.width
        if target_width is not None:
            preferred_width = max(1, int(target_width))
            if fallback_width is not None and image.width < preferred_width:
                preferred_width = max(1, int(fallback_width))
            crop_width = min(image.width, preferred_width)
        crop_height = image.height
        working = image.crop((0, 0, crop_width, crop_height))
        working.load()

    try:
        if bottom_padding is not None:
            content_bottom = _content_bottom(
                working,
                background_tolerance=max(0, min(int(background_tolerance), 254)),
            )
            if content_bottom is not None:
                crop_height = min(
                    working.height,
                    max(1, content_bottom + max(0, int(bottom_padding))),
                )

        changed = crop_width != original_width or crop_height != original_height
        if not changed:
            if not same_path:
                copyfile(rendered_path, out_path)
            return

        cropped = working.crop((0, 0, crop_width, crop_height))
        cropped.load()
        try:
            save_kwargs = {}
            if image_format.upper() in {"JPEG", "JPG"}:
                if cropped.mode not in {"RGB", "L"}:
                    converted = cropped.convert("RGB")
                    cropped.close()
                    cropped = converted
                save_kwargs = {
                    "quality": max(1, min(int(jpeg_quality), 100)),
                    "optimize": True,
                }
            cropped.save(out_path, format=image_format, **save_kwargs)
        finally:
            cropped.close()
    finally:
        working.close()


__all__ = ["save_rendered_image"]
