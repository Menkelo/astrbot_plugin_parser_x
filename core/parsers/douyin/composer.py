import asyncio
import hashlib
import json
from pathlib import Path

from ...data import DynamicContent
from ...utils import exec_ffmpeg_cmd, safe_unlink


class DouyinMediaComposer:
    def __init__(self, downloader, config):
        self.downloader = downloader
        self.config = config

    @staticmethod
    def as_bool(v, default=False) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"1", "true", "yes", "on"}:
                return True
            if s in {"0", "false", "no", "off", ""}:
                return False
        return default

    async def _probe_duration(self, path: Path) -> float:
        """
        使用 ffprobe 获取媒体真实时长，解决 duration=0 或容器时长异常。
        """
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe 获取时长失败: {stderr.decode(errors='ignore')}")

        try:
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            duration = float(data.get("format", {}).get("duration") or 0)
            return max(duration, 0)
        except Exception as e:
            raise RuntimeError(f"ffprobe 解析时长失败: {e}")

    async def _has_audio_stream(self, path: Path) -> bool:
        """
        检查视频自身是否已经带音频。
        如果原动图视频本身有音频，则优先保留原音频，避免重复覆盖。
        """
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return False

        try:
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            streams = data.get("streams") or []
            return bool(streams)
        except Exception:
            return False

    async def _remux_video_keep_av(self, raw_video: Path, final: Path):
        """
        重新封装，修正部分平台动态视频 duration 异常。
        保留原视频内已有音频。
        """
        await exec_ffmpeg_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_video),
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(final),
            ]
        )

    async def _remux_video_no_audio(self, raw_video: Path, final: Path):
        """
        重新封装为无声视频。
        """
        await exec_ffmpeg_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_video),
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
                "-movflags",
                "+faststart",
                str(final),
            ]
        )

    async def compose_dynamic_video_with_bgm(
        self,
        video_url: str,
        vid: str,
        index: int,
        bgm_url: str | None,
        ext_headers: dict[str, str],
    ) -> Path:
        """
        单个抖音动图视频自适应音视频合成。

        逻辑：
        - 先下载单个动图视频；
        - 如果视频自身已有音频，则只重新封装修正时长；
        - 如果视频无音频且作品有 BGM，则循环 BGM 并裁剪到视频真实时长；
        - 如果 BGM 合成失败，则返回重新封装后的无声视频，至少不丢内容。
        """
        cache_dir = Path(self.config.get("cache_dir", "."))
        short = hashlib.md5(f"{vid}|dyn|{index}|{video_url}|{bgm_url}".encode()).hexdigest()[:10]
        work_dir = cache_dir / f"douyin_dyn_{vid}_{index}_{short}"
        work_dir.mkdir(parents=True, exist_ok=True)

        raw_video = await self.downloader.download_video(
            video_url,
            video_name=f"douyin_{vid}_dyn_{index}_raw_{short}.mp4",
            ext_headers=ext_headers,
        )

        if not raw_video.exists() or raw_video.stat().st_size == 0:
            raise RuntimeError("动图视频下载失败")

        try:
            duration = await self._probe_duration(raw_video)
        except Exception:
            duration = 0

        if duration <= 0:
            # 避免 ffmpeg -t 0 导致空文件
            duration = 3.0

        final = work_dir / f"douyin_{vid}_dyn_{index}_{short}.mp4"

        # 如果动图视频源本身有音频，优先保留原音频。
        # 这样不会把原视频声音错误替换成背景音乐。
        has_audio = await self._has_audio_stream(raw_video)
        if has_audio:
            try:
                await self._remux_video_keep_av(raw_video, final)
                await safe_unlink(raw_video)

                if final.exists() and final.stat().st_size > 0:
                    return final
            except Exception:
                await safe_unlink(final)
                # 继续走下面的 BGM/无声兜底

        if bgm_url:
            bgm = None
            try:
                bgm = await self.downloader.download_audio(
                    bgm_url,
                    ext_headers=ext_headers,
                )

                if not bgm.exists() or bgm.stat().st_size == 0:
                    raise RuntimeError("BGM 下载失败")

                # 自适应合成：
                # - 使用视频真实时长；
                # - BGM 不足时循环；
                # - BGM 过长时裁剪；
                # - -shortest 防止音频撑长总时长。
                await exec_ffmpeg_cmd(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(raw_video),
                        "-stream_loop",
                        "-1",
                        "-i",
                        str(bgm),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-t",
                        f"{duration:.3f}",
                        "-shortest",
                        "-movflags",
                        "+faststart",
                        str(final),
                    ]
                )

                await safe_unlink(bgm)

            except Exception:
                if bgm:
                    await safe_unlink(bgm)

                await safe_unlink(final)

                # BGM 合成失败时，至少返回无声视频。
                await self._remux_video_no_audio(raw_video, final)
        else:
            # 没有 BGM 时返回重新封装后的无声视频。
            await self._remux_video_no_audio(raw_video, final)

        await safe_unlink(raw_video)

        if not final.exists() or final.stat().st_size == 0:
            raise RuntimeError("动图视频合成结果无效")

        return final

    def build_dynamic_contents_with_bgm(
        self,
        entries: list[tuple[str, str]],
        vid: str,
        bgm_url: str | None,
        ext_headers: dict[str, str],
    ) -> list[DynamicContent]:
        """
        不合并多个动图视频。
        每个动图视频单独下载、单独自适应合成音频，然后作为一个 DynamicContent 返回。
        """
        contents: list[DynamicContent] = []
        seen: set[str] = set()

        index = 0
        for k, u in entries:
            key = (k or u).strip() if (k or u) else ""
            if not key or not u or key in seen:
                continue

            seen.add(key)
            index += 1

            task = asyncio.create_task(
                self.compose_dynamic_video_with_bgm(
                    video_url=u,
                    vid=vid,
                    index=index,
                    bgm_url=bgm_url,
                    ext_headers=ext_headers,
                )
            )
            contents.append(DynamicContent(task))

        return contents

    def build_unique_dynamic_contents_from_entries(
        self,
        entries: list[tuple[str, str]],
        vid: str,
        ext_headers: dict[str, str],
    ) -> list[DynamicContent]:
        """
        兼容旧调用：仅下载动图视频，不合成 BGM。
        新逻辑推荐使用 build_dynamic_contents_with_bgm。
        """
        contents: list[DynamicContent] = []
        seen: set[str] = set()

        for i, (k, u) in enumerate(entries, start=1):
            key = (k or u).strip() if (k or u) else ""
            if not key or not u or key in seen:
                continue
            seen.add(key)

            short = hashlib.md5(f"{vid}|{i}|{key}|{u}".encode()).hexdigest()[:10]
            name = f"douyin_{vid}_dyn_{i}_{short}.mp4"
            task = self.downloader.download_video(u, video_name=name, ext_headers=ext_headers)
            contents.append(DynamicContent(task))

        return contents

    async def merge_dynamic_videos_with_bgm(
        self,
        entries: list[tuple[str, str]],
        vid: str,
        bgm_url: str | None,
        ext_headers: dict[str, str],
    ) -> Path:
        """
        兼容旧方法：保留，避免其他地方调用报错。
        当前 DouyinParser 不再调用此方法。
        """
        uniq: list[tuple[str, str]] = []
        seen: set[str] = set()
        for k, u in entries:
            key = (k or u).strip() if (k or u) else ""
            if not key or not u or key in seen:
                continue
            seen.add(key)
            uniq.append((key, u))

        if not uniq:
            raise RuntimeError("没有可合并动态视频")

        cache_dir = Path(self.config.get("cache_dir", "."))
        work_dir = cache_dir / f"douyin_merge_{vid}_{hashlib.md5(str(uniq).encode()).hexdigest()[:8]}"
        work_dir.mkdir(parents=True, exist_ok=True)

        tasks = []
        for i, (_, u) in enumerate(uniq, start=1):
            short = hashlib.md5(f"{vid}|seg|{i}|{u}".encode()).hexdigest()[:8]
            tasks.append(
                self.downloader.download_video(
                    u,
                    video_name=f"seg_{i:03d}_{short}.mp4",
                    ext_headers=ext_headers,
                )
            )

        seg_paths = await asyncio.gather(*tasks)
        seg_paths = [p for p in seg_paths if p.exists() and p.stat().st_size > 0]
        if not seg_paths:
            raise RuntimeError("分段下载失败")

        list_file = work_dir / "concat.txt"
        list_file.write_text("\n".join([f"file '{p.as_posix()}'" for p in seg_paths]), encoding="utf-8")

        no_audio = work_dir / f"douyin_{vid}_merged_noaudio.mp4"
        final = work_dir / f"douyin_{vid}_merged.mp4"

        await exec_ffmpeg_cmd(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                str(no_audio),
            ]
        )

        if bgm_url:
            bgm = None
            try:
                bgm = await self.downloader.download_audio(bgm_url, ext_headers=ext_headers)
                await exec_ffmpeg_cmd(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(no_audio),
                        "-stream_loop",
                        "-1",
                        "-i",
                        str(bgm),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-shortest",
                        "-movflags",
                        "+faststart",
                        str(final),
                    ]
                )
                await safe_unlink(bgm)
                await safe_unlink(no_audio)
            except Exception:
                if bgm:
                    await safe_unlink(bgm)
                await safe_unlink(final)
                no_audio.rename(final)
        else:
            await safe_unlink(final)
            no_audio.rename(final)

        await safe_unlink(list_file)
        for p in seg_paths:
            await safe_unlink(p)

        if not final.exists() or final.stat().st_size == 0:
            raise RuntimeError("合并结果无效")

        return final
