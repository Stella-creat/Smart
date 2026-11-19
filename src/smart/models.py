"""SQLAlchemy ORM 模型 — 严格按概要设计 7.3.2 + 映射适配层隐含表设计。

字段与 DeepTutor learning/models.py 的 KnowledgePoint / LearningProgress 语义对齐，
但用 SQLAlchemy 而非 Pydantic，且数据主权在本库。
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smart.db import Base


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ============ 业务枚举 ============


class QuestionType(int, enum.Enum):
    SINGLE = 1   # 单选
    MULTI = 2    # 多选
    JUDGE = 3    # 判断
    FILL = 4     # 填空
    ESSAY = 5    # 简答


class Difficulty(int, enum.Enum):
    EASY = 1
    MEDIUM = 2
    HARD = 3


class QuestionSource(str, enum.Enum):
    IMPORT = "IMPORT"
    AI = "AI"
    MANUAL = "MANUAL"


class TaskStatus(int, enum.Enum):
    TODO = 0
    DONE = 1
    SKIPPED = 2
    CARRIED = 3  # 已顺延：原日期保留记录，任务副本已重排到之后的日期


class TaskType(str, enum.Enum):
    LEARN = "LEARN"
    REVIEW = "REVIEW"
    PRACTICE = "PRACTICE"


class KnowledgeType(str, enum.Enum):
    MEMORY = "memory"
    CONCEPT = "concept"
    PROCEDURE = "procedure"
    DESIGN = "design"


# ============ 用户 ============


class User(Base):
    __tablename__ = "t_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="student")  # student / admin
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


# ============ 知识点树 ============


class KnowledgePoint(Base):
    """三级知识点树（科目/章节/名称），挂载掌握度与遗忘曲线状态。"""

    __tablename__ = "t_knowledge_point"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    # 0 表示系统内置（所有用户可见，不可删除）
    subject: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    chapter: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("t_knowledge_point.id"), nullable=True)
    knowledge_type: Mapped[KnowledgeType] = mapped_column(String(16), default=KnowledgeType.CONCEPT.value)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)  # 知识点解析（首次 RAG 检索后缓存）

    # === 掌握度（0-100） + 遗忘曲线状态 ===
    mastery: Mapped[float] = mapped_column(Float, default=0.0)
    mastery_count: Mapped[int] = mapped_column(Integer, default=0)  # 累计答题次数（用于 confidence cap）
    last_review_time: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    next_review_time: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_correct: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_wrong: Mapped[int] = mapped_column(Integer, default=0)
    interval_index: Mapped[int] = mapped_column(Integer, default=0)  # 遗忘曲线当前间隔位置

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    parent: Mapped["KnowledgePoint | None"] = relationship(remote_side=[id])
    children: Mapped[list["KnowledgePoint"]] = relationship(back_populates="parent", cascade="all, delete-orphan")


# ============ 题库 ============


class QuestionBank(Base):
    """题库 —— 按科目（关联知识库）划分，一库一组题目。"""

    __tablename__ = "t_question_bank"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 关联的知识库 / 科目名
    kb_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class Question(Base):
    __tablename__ = "t_question"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    # 0 表示系统内置题库
    knowledge_point_id: Mapped[int] = mapped_column(ForeignKey("t_knowledge_point.id"), index=True, nullable=False)
    # 所属题库；历史题目为 NULL（首次调用题库列表时按科目回填）
    bank_id: Mapped[int | None] = mapped_column(
        ForeignKey("t_question_bank.id"), index=True, nullable=True
    )

    type: Mapped[QuestionType] = mapped_column(Integer, nullable=False)
    difficulty: Mapped[Difficulty] = mapped_column(Integer, nullable=False, default=Difficulty.MEDIUM)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)      # JSON 数组，单选/多选
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[QuestionSource] = mapped_column(String(16), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    knowledge_point: Mapped[KnowledgePoint] = relationship()
    bank: Mapped["QuestionBank | None"] = relationship()


# ============ 学习计划 + 计划任务 ============


class LearningPlan(Base):
    __tablename__ = "t_learning_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(100), nullable=False)
    exam_date: Mapped[dt.date] = mapped_column(nullable=True)
    daily_minutes: Mapped[int] = mapped_column(Integer, default=60)
    duration_days: Mapped[int] = mapped_column(Integer, default=14)
    status: Mapped[int] = mapped_column(Integer, default=1)  # 1 进行中 0 已完成

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class PlanTask(Base):
    """每日任务清单 —— 复习任务 + 练习任务双轨。"""

    __tablename__ = "t_plan_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    plan_id: Mapped[int] = mapped_column(ForeignKey("t_learning_plan.id"), index=True, nullable=False)
    knowledge_point_id: Mapped[int] = mapped_column(ForeignKey("t_knowledge_point.id"), index=True, nullable=False)

    task_type: Mapped[TaskType] = mapped_column(String(16), nullable=False)  # REVIEW / PRACTICE
    task_date: Mapped[dt.date] = mapped_column(nullable=False, index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=15)
    status: Mapped[TaskStatus] = mapped_column(Integer, default=TaskStatus.TODO.value)
    actual_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 练习任务专属：关联的题目 ID 列表
    question_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# ============ 映射适配层三张表 ============


class MappingAlias(Base):
    """人工沉淀的别名表（L2 匹配来源）。"""

    __tablename__ = "t_mapping_alias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    kp_id: Mapped[int] = mapped_column(ForeignKey("t_knowledge_point.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="MANUAL")  # MANUAL / REVIEW_AUTO
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class MappingStaging(Base):
    """暂存表 — 映射结果隔离区，校验不通过不影响业务库。"""

    __tablename__ = "t_mapping_staging"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)      # DEEPTUTOR_CHUNK / DOC_TITLE / ...
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)     # 外部引用
    source_text: Mapped[str] = mapped_column(Text, nullable=True)            # 用于相似度计算的文本
    kp_id: Mapped[int] = mapped_column(ForeignKey("t_knowledge_point.id"), nullable=False)
    match_level: Mapped[int] = mapped_column(Integer, nullable=False)        # 1/2/3/4
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[int] = mapped_column(Integer, default=0)                  # 0 待处理 1 已通过 2 已拒绝
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class MappingChunkPoint(Base):
    """正式映射表 — 业务层查询唯一入口。"""

    __tablename__ = "t_mapping_chunk_point"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_ref: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    kp_id: Mapped[int] = mapped_column(ForeignKey("t_knowledge_point.id"), index=True, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


# ============ 答题记录（供 mastery + 遗忘曲线使用）============


class AttemptRecord(Base):
    """每次答题/复习打卡的记录 —— mastery 算法的原始数据源。"""

    __tablename__ = "t_attempt_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    knowledge_point_id: Mapped[int] = mapped_column(
        ForeignKey("t_knowledge_point.id"), index=True, nullable=False
    )
    task_id: Mapped[int | None] = mapped_column(ForeignKey("t_plan_task.id"), nullable=True)
    is_correct: Mapped[bool] = mapped_column(nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


# ============ 练习记录（逐题作答明细）============


class PracticeRecord(Base):
    """练习任务内每道题的作答明细 —— 支持断点续练与任务结算。"""

    __tablename__ = "t_practice_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    task_id: Mapped[int] = mapped_column(ForeignKey("t_plan_task.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(ForeignKey("t_question.id"), index=True, nullable=False)

    user_answer: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    is_correct: Mapped[bool] = mapped_column(nullable=False)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


# ============ 知识库资源（文档 / 视频 / 链接）============


class KbResourceKind(str, enum.Enum):
    DOC = "doc"
    VIDEO = "video"
    LINK = "link"


class KBResource(Base):
    """知识库附属资源 —— 文档与链接进 DeepTutor 索引，视频仅在业务侧留存。"""

    __tablename__ = "t_kb_resource"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    kb_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class FeedbackStatus(str, enum.Enum):
    OPEN = "open"
    REPLIED = "replied"
    CLOSED = "closed"


class Feedback(Base):
    """用户反馈 / 客服工单 —— 使用者提交的问题与建议。"""

    __tablename__ = "t_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=FeedbackStatus.OPEN.value)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
