import pytest
from core.parsers.ytdlp import AcFunParser


def test_actual_acfun_captioned_image_ubb_maps_to_gallery(tmp_path):
    parser = AcFunParser({'cache_dir':str(tmp_path)}, object())
    entry=parser.comment_feed._entry({'content':'看图[img=图片]https://imgs.aixifan.com/a.jpeg[/img]'})
    assert entry.images == ['https://imgs.aixifan.com/a.jpeg']
    assert ''.join(p.text for p in entry.content) == '看图'
