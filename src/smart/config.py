"""全局配置 — 从环境变量或 .env 加载。"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 应用 ---
    app_name: str = "智能知识库与个性化学习管理系统"
    debug: bool = True

    # --- 数据库（MySQL 生产 / SQLite 开发） ---
    # 用绝对路径避免 cwd 不同（后台协程 vs HTTP handler）导致 SQLite 找不到 DB
    db_url: str = "sqlite:///D:/Smart/smart_dev.db"
    # db_url: str = "mysql+aiomysql://user:pass@localhost:3306/smart?charset=utf8mb4"

    # --- DeepTutor 外部引擎 ---
    deeptutor_base_url: str = "http://localhost:8001"
    deeptutor_ws_url: str = "ws://localhost:8001/ws"
    deeptutor_timeout: float = 15.0

    # --- JWT ---
    jwt_secret: str = "change-me-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24h

    # --- 映射适配层参数 ---
    semantic_similarity_threshold_auto: float = 0.85   # ≥ 自动入库
    semantic_similarity_threshold_review: float = 0.60  # ≥ 进入人工审核

    # --- 每日定时回流 ---
    # 注意：SQLite 多线程（FastAPI worker + daily_loop 协程）会有锁竞争，
    # 生产环境建议用 MySQL + 独立 cron 任务；开发期先关掉。
    daily_rollover_enabled: bool = False
    daily_rollover_hour: int = 0                     # 本地时间几点后可触发
    daily_rollover_interval_seconds: int = 300       # 巡检间隔


settings = Settings()
