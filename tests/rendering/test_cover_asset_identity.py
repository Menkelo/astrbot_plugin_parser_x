import pytest
from astrbot_plugin_parser_x.core.card_flow import build_card_extra

ASSET='owy5m0g3RiAVgAC1AvArINEXfsADXCADiB8eai'
COVER=f'https://p26-sign.douyinpic.com/tos-cn-i-0813c001/{ASSET}~tplv-dy-resize-walign-adapt-aq:540:q75.webp?signature=cover'
ORIGINAL=f'https://p3-sign.douyinpic.com/tos-cn-i-0813c001/{ASSET}~tplv-dy-lqen-new:1440:1920:q80.webp?signature=original'

def images(extra):
    return [b for b in extra.get('text_card_flow',[]) if b['type']=='image']

def test_douyin_same_asset_cover_and_original_not_repeated_in_body():
    extra=build_card_extra([('text','你好能进来吗。'),('image',ORIGINAL)],cover=COVER)
    assert extra['text_card_media']==COVER
    assert extra['text_card_media_fit']=='cover'
    assert images(extra)==[]
    assert extra['text_card_flow']==[{'type':'text','text':'你好能进来吗。'}]

@pytest.mark.parametrize('original',[
    ORIGINAL.replace(ASSET,'differentAsset'),
    ORIGINAL.replace('tos-cn-i-0813c001','tos-cn-i-other'),
    ORIGINAL.replace('p3-sign.douyinpic.com','images.example.com'),
    ORIGINAL.replace('p3-sign.douyinpic.com','douyinpic.com.evil.test'),
])
def test_distinct_or_untrusted_asset_keeps_body_image(original):
    assert len(images(build_card_extra([('text','正文'),('image',original)],cover=COVER)))==1

def test_interleaved_images_without_explicit_cover_still_preserved():
    extra=build_card_extra([('text','一'),('image',ORIGINAL),('text','二'),('image','https://example.com/second.png')])
    assert len(images(extra))==2

def test_stacked_images_still_detached():
    extra=build_card_extra([('text','正文'),('image',ORIGINAL),('image','https://example.com/second.png')],cover=COVER)
    assert extra['text_card_detach_images'] is True
    assert images(extra)==[]
