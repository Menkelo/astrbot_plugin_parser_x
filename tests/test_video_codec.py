import pytest
from astrbot_plugin_parser_x.core.video_codec import normalize_codec, codec_preference, prefer_candidates, ytdlp_video_format

@pytest.mark.parametrize('value,expected',[('avc1.640028','avc'),('hvc1.1.6','hevc'),('H.265','hevc'),('vp9',''),('auto','')])
def test_codec_names(value,expected):
    assert normalize_codec(value)==expected

def test_preference_falls_back_without_target():
    items=[{'codec':'avc1.4'},{'codec':'vp9'}]
    assert prefer_candidates(items,'hevc',lambda x:x['codec'])==items
    assert prefer_candidates(items,'avc',lambda x:x['codec'])==items[:1]
    assert codec_preference({'performance':{'video_codec':'auto'}})=='auto'

def test_ytdlp_preserves_auto_and_fallback():
    original='best[height<=720]/bestvideo[height<=720]+bestaudio/best'
    assert ytdlp_video_format('auto')==original
    assert ytdlp_video_format('hevc').endswith('/'+original)
    assert 'vcodec~=' in ytdlp_video_format('avc')
