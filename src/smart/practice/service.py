"""练习模块 —— 组卷、答题提交、任务结算。

闭环：计划任务（PRACTICE）→ 配题（题库优先，不足时 AI 补题）
→ 逐题提交（复用 QuestionService 判分）→ 全部答完自动结算
→ 写 t_practice_record + 回流 t_knowledge_point.mastery / 遗忘曲线。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from smart import models
from smart.adapter.provider.ai_provider import AIProvider
from smart.db_utils import to_dict
from smart.knowledge.service import KnowledgeService
from smart.plan.service import PlanService
from smart.question.service import QuestionService

DEFAULT_PAPER_SIZE = 3
PASS_RATIO = 0.6


class PracticeService:
    """练习执行服务。"""

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ===== 1. 组卷 =====

    def force_generate(self, db: Session, user_id: int, task_id: int, count: int = 3) -> dict:
        """任务暂无题目时，强制调用 AI 为该知识点生成练习并挂到任务上。"""
        task = self._get_task(db, user_id, task_id)
        kp = db.get(models.KnowledgePoint, task.knowledge_point_id)
        if kp is None:
            raise ValueError(f"知识点 {task.knowledge_point_id} 不存在")

        try:
            chunks = KnowledgeService(self.provider).pull_chunks_for_matching(
                kp.subject, kp.name, top_k=8
            )
        except Exception:
            chunks = []

        saved = QuestionService(self.provider).ai_generate(
            db, user_id, kp.id, chunks, types=[1, 3], difficulty=2, count=count,
        )
        if not saved:
            raise ValueError(f"AI 未能为知识点「{kp.name}」生成题目，请稍后重试")

        ids = list(task.question_ids or [])
        for q in saved:
            if q.id not in ids:
                ids.append(q.id)
        task.question_ids = ids
        db.commit()
        db.refresh(task)
        return {"task_id": task.id, "generated": len(saved), "question_ids": ids}

    def generate_for_kp(self, db: Session, user_id: int, kp_id: int, count: int = 3) -> dict:
        """知识点没有练习任务时：确保存在今日任务，再 AI 生成题目。"""
        kp = db.get(models.KnowledgePoint, kp_id)
        if kp is None:
            raise ValueError("知识点不存在")
        plan = db.execute(
            select(models.LearningPlan).where(
                models.LearningPlan.user_id == user_id,
                models.LearningPlan.subject == kp.subject,
            )
        ).scalars().first()
        if plan is None:
            raise ValueError(f"知识点所属知识库「{kp.subject}」未创建学习计划，无法生成练习")

        today = dt.date.today()
        task = db.execute(
            select(models.PlanTask).where(
                models.PlanTask.user_id == user_id,
                models.PlanTask.knowledge_point_id == kp_id,
                models.PlanTask.task_date == today,
            )
        ).scalars().first()
        if task is None:
            task = models.PlanTask(
                user_id=user_id, plan_id=plan.id, knowledge_point_id=kp_id,
                task_type=(
                    models.TaskType.REVIEW.value
                    if (kp.mastery or 0) > 0 else models.TaskType.LEARN.value
                ),
                task_date=today, duration_minutes=15,
                status=models.TaskStatus.TODO.value, question_ids=[],
            )
            db.add(task)
            db.commit()
            db.refresh(task)
        return self.force_generate(db, user_id, task.id, count)

    def build_paper(
        self, db: Session, user_id: int, task_id: int, size: int = DEFAULT_PAPER_SIZE
    ) -> tuple[models.PlanTask, list[models.Question]]:
        """确保任务已配题并返回试卷（题库不足时调用 AI 补题）。"""
        task = self._get_task(db, user_id, task_id)
        questions = self._load_questions(db, task.question_ids)
        if questions:
            return task, questions

        kp = db.get(models.KnowledgePoint, task.knowledge_point_id)
        if kp is None:
            raise ValueError(f"知识点 {task.knowledge_point_id} 不存在")

        qsvc = QuestionService(self.provider)
        picked = qsvc.pick_for_kp(db, kp.id, kp.mastery, size)
        if len(picked) < size:
            try:
                chunks = KnowledgeService(self.provider).pull_chunks_for_matching(
                    kp.subject, kp.name, top_k=8
                )
                qsvc.ai_generate(
                    db, user_id, kp.id, chunks,
                    types=[1, 3], difficulty=2, count=size - len(picked),
                )
            except Exception:
                # AI 不可用时降级为纯题库配题
                pass
            picked = qsvc.pick_for_kp(db, kp.id, kp.mastery, size)
        if not picked:
            raise ValueError(f"知识点「{kp.name}」暂无可用题目，请先录入或 AI 出题")

        task.question_ids = [q.id for q in picked]
        db.commit()
        db.refresh(task)
        return task, picked

    def paper(
        self, db: Session, user_id: int, task_id: int, size: int = DEFAULT_PAPER_SIZE
    ) -> dict:
        """返回试卷（不下发答案），并附带已答状态，支持断点续练。"""
        task, questions = self.build_paper(db, user_id, task_id, size)
        records = self._records(db, task_id)
        answered = {r.question_id: r for r in records}

        items = []
        for idx, q in enumerate(questions, 1):
            r = answered.get(q.id)
            items.append({
                "index": idx,
                "question_id": q.id,
                "type": q.type,
                "difficulty": q.difficulty,
                "content": q.content,
                "options": q.options,
                "answered": r is not None,
                "user_answer": r.user_answer if r else None,
                "is_correct": r.is_correct if r else None,
                "score": r.score if r else None,
            })
        return {
            "task": to_dict(task),
            "questions": items,
            "progress": self._progress(records, len(questions)),
        }

    # ===== 2. 逐题提交 =====

    def submit(
        self,
        db: Session,
        user_id: int,
        task_id: int,
        question_id: int,
        user_answer: str,
        duration_seconds: int | None = None,
    ) -> dict:
        """判分并记录；全部答完时自动结算任务。"""
        task = self._get_task(db, user_id, task_id)
        if question_id not in (task.question_ids or []):
            raise ValueError(f"题目 {question_id} 不属于练习任务 {task_id}")

        judgment, _ = QuestionService(self.provider).submit_and_judge(
            db, user_id, question_id, user_answer,
            task_id=task_id, duration_seconds=duration_seconds,
        )

        record = self._record(db, task_id, question_id)
        if record is None:
            record = models.PracticeRecord(
                user_id=user_id, task_id=task_id, question_id=question_id,
            )
            db.add(record)
        record.user_answer = user_answer
        record.score = judgment.score
        record.is_correct = judgment.is_correct
        record.feedback = judgment.feedback
        record.duration_seconds = duration_seconds
        db.commit()
        db.refresh(record)

        progress = self._progress(self._records(db, task_id), len(task.question_ids or []))
        finished = False
        if progress["finished"]:
            task = self.finish(db, user_id, task_id)
            finished = True

        # 查询最新知识点掌握度（已在 finish 中回流）
        kp = db.get(models.KnowledgePoint, task.knowledge_point_id)
        kp_mastery = float(kp.mastery) if kp and kp.mastery is not None else 0.0

        return {
            "score": judgment.score,
            "is_correct": judgment.is_correct,
            "feedback": judgment.feedback,
            "record": to_dict(record),
            "progress": progress,
            "finished": finished,
            "kp_mastery": kp_mastery,
            "kp_name": kp.name if kp else None,
            "task": to_dict(task),
        }

    # ===== 3. 任务结算 =====

    def finish(self, db: Session, user_id: int, task_id: int) -> models.PlanTask:
        """按本次全部答题结果回流掌握度与遗忘曲线。"""
        task = self._get_task(db, user_id, task_id)
        records = self._records(db, task_id)
        if not records:
            raise ValueError("尚无答题记录，无法结算")

        total_seconds = sum(r.duration_seconds or 0 for r in records)
        actual_minutes = round(total_seconds / 60) or None
        correctness = [bool(r.is_correct) for r in records]

        return PlanService().apply_practice_result(
            db, task, correctness, actual_minutes=actual_minutes
        )

    def records(self, db: Session, user_id: int, task_id: int) -> list[dict]:
        task = self._get_task(db, user_id, task_id)
        return [to_dict(r) for r in self._records(db, task.id)]

    # ===== 内部工具 =====

    def _get_task(self, db: Session, user_id: int, task_id: int) -> models.PlanTask:
        task = db.get(models.PlanTask, task_id)
        if task is None or task.user_id != user_id:
            raise ValueError(f"练习任务 {task_id} 不存在")
        if task.task_type not in (
            models.TaskType.LEARN.value,
            models.TaskType.REVIEW.value,
            models.TaskType.PRACTICE.value,
        ):
            raise ValueError(f"任务 {task_id} 不支持练习")
        return task

    def _load_questions(self, db: Session, ids: list[int] | None) -> list[models.Question]:
        if not ids:
            return []
        found = {
            q.id: q
            for q in db.execute(
                select(models.Question).where(models.Question.id.in_(ids))
            ).scalars().all()
        }
        # 保持配题时的顺序
        return [found[i] for i in ids if i in found]

    def _records(self, db: Session, task_id: int) -> list[models.PracticeRecord]:
        return list(db.execute(
            select(models.PracticeRecord)
            .where(models.PracticeRecord.task_id == task_id)
            .order_by(models.PracticeRecord.id)
        ).scalars().all())

    def _record(
        self, db: Session, task_id: int, question_id: int
    ) -> models.PracticeRecord | None:
        return db.execute(
            select(models.PracticeRecord).where(
                models.PracticeRecord.task_id == task_id,
                models.PracticeRecord.question_id == question_id,
            )
        ).scalars().first()

    def _progress(self, records: list[models.PracticeRecord], total: int) -> dict:
        answered = len({r.question_id for r in records})
        correct = sum(1 for r in records if r.is_correct)
        return {
            "total": total,
            "answered": answered,
            "correct": correct,
            "accuracy": round(correct / answered, 2) if answered else 0.0,
            "finished": total > 0 and answered >= total,
        }
