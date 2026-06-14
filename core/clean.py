import asyncio
import time
import zoneinfo
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context


class CacheCleaner:
    """
    每天固定时间自动清理插件缓存目录的调度器封装。
    """

    JOBNAME = "CacheCleaner"
    RETENTION_HOURS = 24
    MAX_AGE_SECONDS = RETENTION_HOURS * 60 * 60

    def __init__(self, context: Context, config: AstrBotConfig):
        # 内嵌清理周期：每天 00:00
        self.clean_cron = "0 0 * * *"
        self.cache_dir = Path(config["cache_dir"])

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        self.register_task()

        logger.info(
            f"{self.JOBNAME} 已启动，任务周期: {self.clean_cron}，"
            f"清理超过 {self.RETENTION_HOURS} 小时的缓存"
        )

    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(
                self.clean_cron,
                timezone=self.timezone,
            )
            self.scheduler.add_job(
                func=self._clean_plugin_cache,
                trigger=self.trigger,
                name=f"{self.JOBNAME}_scheduler",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
            )
        except Exception as e:
            logger.error(f"[{self.JOBNAME}] Cron 格式错误：{e}")

    def _clean_expired_files(self) -> tuple[int, int]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - self.MAX_AGE_SECONDS
        removed_files = 0
        removed_dirs = 0

        for path in self.cache_dir.rglob("*"):
            try:
                if not path.is_file():
                    continue
                if path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
                removed_files += 1
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.debug(f"[{self.JOBNAME}] skip cache file {path}: {e}")

        dirs = sorted(
            (p for p in self.cache_dir.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for path in dirs:
            try:
                path.rmdir()
                removed_dirs += 1
            except OSError:
                continue
            except Exception as e:
                logger.debug(f"[{self.JOBNAME}] skip cache dir {path}: {e}")

        return removed_files, removed_dirs

    async def _clean_plugin_cache(self) -> None:
        """Clean only expired cache files, keeping active downloads/renders intact."""
        loop = asyncio.get_running_loop()
        try:
            removed_files, removed_dirs = await loop.run_in_executor(
                None,
                self._clean_expired_files,
            )
            logger.info(
                f"Cache cleanup finished: retention={self.RETENTION_HOURS}h, "
                f"files={removed_files}, dirs={removed_dirs}"
            )
        except Exception:
            logger.exception("Error while cleaning cache directory.")

    async def stop(self):
        try:
            self.scheduler.remove_all_jobs()
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception:
            logger.exception(f"[{self.JOBNAME}] 停止时发生异常")
        logger.info(f"[{self.JOBNAME}] 已停止")
