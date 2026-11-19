"""每日定时回流 —— 跨日推进学习计划。

启动后由 FastAPI startup 拉起一个协程，按 daily_rollover_hour 触发，
每个自然日最多执行一次；同进程内单实例，多 worker 部署时需改为分布式锁。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from smart.config import settings
from smart.db import SessionLocal
from smart.plan.service import PlanService

logger = logging.getLogger(__name__)


def _ensure_handler() -> None:
    """uvicorn 未给本项目 logger 配 handler，这里补一个以免日志被丢弃。"""
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

_last_run: dt.date | None = None


def run_once(target_date: dt.date | None = None) -> dict:
    """执行一次回流（同步，供定时协程与手动补跑共用）。"""
    with SessionLocal() as db:
        return PlanService().run_daily_rollover(db, target_date)


async def daily_loop() -> None:
    """常驻巡检：到达设定时刻且当天未跑过时才执行。"""
    global _last_run
    _ensure_handler()
    interval = max(30, settings.daily_rollover_interval_seconds)

    while True:
        try:
            now = dt.datetime.now()
            if now.hour >= settings.daily_rollover_hour and _last_run != now.date():
                result = run_once(now.date())
                _last_run = now.date()
                logger.info("每日回流完成: %s", result)
        except Exception:  # 定时任务不能中断常驻协程
            logger.exception("每日回流执行失败")
        await asyncio.sleep(interval)
