"""SQLAlchemy 数据库连接与会话管理。"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from smart.config import settings

connect_args = {"check_same_thread": False, "timeout": 30} if settings.db_url.startswith("sqlite") else {}
engine = create_engine(
    settings.db_url, echo=settings.debug, future=True,
    connect_args=connect_args,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    if engine.dialect.name == "sqlite":
        # 每个 session 打开后立刻设置 PRAGMA（每个连接都需要独立设）
        db.execute(text("PRAGMA journal_mode=DELETE"))
        db.execute(text("PRAGMA busy_timeout=30000"))
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """首次运行时建表（后续迁移用 Alembic，这里仅开发期用）。"""
    from smart import models  # noqa: F401 — 触发模型注册到 Base.metadata

    Base.metadata.create_all(bind=engine)

    # SQLite 多线程稳定性：切 DELETE journal（WAL 模式需要 .db-wal / .db-shm 且易权限问题）+ 更长 busy_timeout
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=DELETE"))
            conn.execute(text("PRAGMA busy_timeout=30000"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))

    _migrate_sqlite()


def _migrate_sqlite() -> None:
    """开发期轻量迁移：给已存在的表补齐新增列（create_all 不会改既有表）。"""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(t_learning_plan)"))}
        if cols and "duration_days" not in cols:
            conn.execute(text("ALTER TABLE t_learning_plan ADD COLUMN duration_days INTEGER DEFAULT 14"))
        kp_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(t_knowledge_point)"))}
        if kp_cols and "content" not in kp_cols:
            conn.execute(text("ALTER TABLE t_knowledge_point ADD COLUMN content TEXT"))
        q_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(t_question)"))}
        if q_cols and "bank_id" not in q_cols:
            conn.execute(text("ALTER TABLE t_question ADD COLUMN bank_id INTEGER"))
