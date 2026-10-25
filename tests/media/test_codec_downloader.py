from pathlib import Path
import pytest
from core.download import Downloader

@pytest.mark.asyncio
@pytest.mark.parametrize('preference',['auto','avc','hevc'])
async def test_ytdlp_download_selects_source_without_converter(tmp_path,monkeypatch,preference):
    options=[]
    class YDL:
        def __init__(self,opts): self.opts=opts; options.append(opts)
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def extract_info(self,*args,**kwargs): return {'title':'T'}
        def download(self,urls):
            Path(self.opts['outtmpl'].replace('%(ext)s','mp4')).write_bytes(b'x'*200)
    monkeypatch.setattr('core.download.yt_dlp.YoutubeDL',YDL)
    downloader=Downloader({'cache_dir':str(tmp_path),'performance':{'video_codec':preference}})
    try:
        result=await downloader._ytdlp_download_video('https://example.com/video')
        actual=options[-1]
        assert not actual.get('postprocessors')
        assert 'height<=720' in actual['format']
        assert ('vcodec~=' in actual['format']) == (preference!='auto')
        assert (f'_codec_{preference}' in result.name) == (preference!='auto')
    finally:
        await downloader.close()
