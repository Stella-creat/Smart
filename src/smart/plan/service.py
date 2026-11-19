"""学习计划模块 —— 排期算法 + 双轨任务生成 + 回流。

核心算法直接复刻 DeepTutor learning/ 子模块的成熟实现：
- mastery.py  的 recency-weighted accuracy + confidence cap
- scheduler.py 的间隔重复（按 KnowledgeType 分间隔序列）
但数据模型适配业务层 SQLAlchemy 表（答题记录 t_attempt_record、任务 t_plan_task）。
"""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from smart import models
from smart.adapter.container import get_ai_provider


# ============ 复刻 DeepTutor mastery + scheduler 的核心常量 ============

def _as_date(v) -> dt.date | None:
    """DateTime 列在 SQLite 下可能返回 date 或 datetime，统一转 date。"""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    return v


_RECENCY_WEIGHTS: tuple[float, ...] = (0.5, 0.7, 0.85, 0.95, 1.0)
_CONFIDENCE_CAP: dict[int, float] = {1: 0.5, 2: 0.8}

_INTERVAL_DAYS: dict[str, list[int]] = {
    "memory":     [0, 1, 3, 7, 14, 30, 60],
    "concept":    [3, 7, 14, 30],
    "procedure":  [3, 7, 14],
    "design":     [14, 28],
}

# 排期用的「复习阶梯」：相对首次学习日的第几天安排复习（间隔重复/遗忘曲线）
# 由 _INTERVAL_DAYS 的相邻间隔累加得到（忽略首项的 0）
_REVIEW_LADDER: dict[str, list[int]] = {
    "memory":    [1, 4, 11, 25, 55],
    "concept":   [3, 10, 24, 54],
    "procedure": [3, 10, 24],
    "design":    [14, 42],
}
_LEARN_MINUTES = 20
_REVIEW_MINUTES = 10
# 每日复习数量上限：超出部分顺延到次日（逾期节点会在后续天补排）
_MAX_REVIEWS_PER_DAY = 4


def compute_mastery(correctness: list[bool]) -> float:
    """从答题对错历史计算 mastery（0..1）。复刻 DeepTutor mastery.py。"""
    if not correctness:
        return 0.0
    recent = correctness[-len(_RECENCY_WEIGHTS):]
    weights = _RECENCY_WEIGHTS[-len(recent):]
    score = sum(w * (1.0 if c else 0.0) for c, w in zip(recent, weights)) / sum(weights)
    return min(score, _CONFIDENCE_CAP.get(len(recent), 1.0))


class PlanService:
    """业务层学习计划服务。"""

    def __init__(self) -> None:
        pass

    # ===== 1. 生成学习计划 =====

    def create_plan(
        self,
        db: Session,
        user_id: int,
        subject: str,
        exam_date: dt.date | None,
        daily_minutes: int,
        duration_days: int = 14,
    ) -> models.LearningPlan:
        # 一个知识库（科目）只允许关联一个学习计划
        exists = db.execute(
            select(models.LearningPlan).where(
                models.LearningPlan.user_id == user_id,
                models.LearningPlan.subject == subject,
            )
        ).scalars().first()
        if exists is not None:
            raise ValueError(f"「{subject}」已有学习计划（#{exists.id}），一个知识库只能建一个计划")

        plan = models.LearningPlan(
            user_id=user_id, subject=subject, exam_date=exam_date,
            daily_minutes=daily_minutes, duration_days=max(1, duration_days),
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
        # 按用户自定义天数生成整个周期的任务
        self._generate_tasks(db, plan, horizon_days=max(1, duration_days))
        return plan

    def delete_plan(self, db: Session, user_id: int, plan_id: int) -> bool:
        """删除计划及其全部任务（知识点与答题记录保留），删除后可重新建计划。"""
        plan = db.execute(
            select(models.LearningPlan).where(
                models.LearningPlan.id == plan_id,
                models.LearningPlan.user_id == user_id,
            )
        ).scalars().first()
        if plan is None:
            return False
        db.execute(delete(models.PlanTask).where(models.PlanTask.plan_id == plan_id))
        db.delete(plan)
        db.commit()
        return True

    def _generate_tasks(
        self, db: Session, plan: models.LearningPlan, horizon_days: int = 14
    ) -> None:
        """首次建计划时，预生成未来 N 天的任务清单。"""
        today = dt.date.today()
        for offset in range(horizon_days):
            self.generate_day_tasks(
                db, plan, today + dt.timedelta(days=offset), only_missing=False
            )

    def generate_day_tasks(
        self,
        db: Session,
        plan: models.LearningPlan,
        task_date: dt.date,
        only_missing: bool = True,
    ) -> list[models.PlanTask]:
        """为指定日期生成任务：① 到期复习（优先） ② 新知识点学习（受限）。

        排期原则：
        - Day 1 不排复习（没有已学知识点）
        - 最后一天不排新学（只剩复习 + 总结）
        - 新知识点按「剩余知识点 / 剩余可排新学天数」均分到每天
        - 已学过的按遗忘曲线阶梯（_REVIEW_LADDER）在到期日排复习
        - REVIEW 任务优先消费时间预算，剩下的才给 LEARN
        - only_missing=True 时幂等：当日已有同知识点同类型任务则跳过
        """
        user_kps = list(db.execute(
            select(models.KnowledgePoint).where(
                (models.KnowledgePoint.user_id == plan.user_id)
                | (models.KnowledgePoint.user_id == 0)
            ).where(models.KnowledgePoint.subject == plan.subject)
        ).scalars().all())
        if not user_kps:
            return []

        # 按章节、创建顺序排布，保证「依次加入新内容」
        real_kps = [
            kp for kp in user_kps
            if not (kp.chapter == "知识库" and kp.name == plan.subject)
        ]
        ordered_kps = sorted(real_kps or user_kps, key=lambda kp: (kp.chapter or "", kp.id))

        existing: set[tuple[int, str]] = set()
        if only_missing:
            rows = db.execute(
                select(models.PlanTask.knowledge_point_id, models.PlanTask.task_type).where(
                    models.PlanTask.plan_id == plan.id,
                    models.PlanTask.task_date == task_date,
                )
            ).all()
            existing = {(kp_id, str(ttype)) for kp_id, ttype in rows}

        # 本计划里每个知识点的首次学习日（只看今天之前）
        learned_rows = db.execute(
            select(
                models.PlanTask.knowledge_point_id,
                func.min(models.PlanTask.task_date),
            ).where(
                models.PlanTask.plan_id == plan.id,
                models.PlanTask.task_date < task_date,
                models.PlanTask.task_type == models.TaskType.LEARN.value,
            ).group_by(models.PlanTask.knowledge_point_id)
        ).all()
        learn_dates = {kp_id: _as_date(d) for kp_id, d in learned_rows}

        # 今天已经排过的知识点（不论类型）——用于避免同一天重复排
        today_ids = {kp_id for kp_id, _ in existing}

        created: list[models.PlanTask] = []
        day_index = (task_date - plan.created_at.date()).days + 1 if plan.created_at else 1
        is_first = day_index == 1
        is_last = day_index >= plan.duration_days

        # ===== ② 复习任务先排（优先级高；单日有上限，超出顺延到次日） =====
        remaining_minutes = plan.daily_minutes
        if not is_first:
            # 每个知识点历史上已排过的复习次数（不含今天），用于判断阶梯节点是否已完成
            done_rows = db.execute(
                select(
                    models.PlanTask.knowledge_point_id,
                    func.count(),
                ).where(
                    models.PlanTask.plan_id == plan.id,
                    models.PlanTask.task_date < task_date,
                    models.PlanTask.task_type == models.TaskType.REVIEW.value,
                ).group_by(models.PlanTask.knowledge_point_id)
            ).all()
            done_reviews = {kp_id: n for kp_id, n in done_rows}

            due: list[tuple[dt.date, models.KnowledgePoint]] = []
            for kp in ordered_kps:
                if kp.id not in learn_dates:
                    continue
                delta = (task_date - learn_dates[kp.id]).days
                if delta <= 0:
                    continue
                kp_type = getattr(kp.knowledge_type, "value", kp.knowledge_type)
                ladder = _REVIEW_LADDER.get(kp_type, _REVIEW_LADDER["concept"])
                due_nodes = [n for n in ladder if n <= delta]
                done = done_reviews.get(kp.id, 0)
                if done >= len(due_nodes):
                    continue
                # 最早一个未完成的到期节点（因上限被顺延的过期节点会在今天补上）
                target = learn_dates[kp.id] + dt.timedelta(days=due_nodes[done])
                due.append((target, kp))

            # 逾期最久的优先补排；同一天到期按知识点顺序
            due.sort(key=lambda x: (x[0], x[1].id))

            review_count = 0
            for _, kp in due:
                if review_count >= _MAX_REVIEWS_PER_DAY:
                    break
                if remaining_minutes < _REVIEW_MINUTES:
                    break
                if (kp.id, models.TaskType.REVIEW.value) in existing:
                    continue
                task = models.PlanTask(
                    user_id=plan.user_id, plan_id=plan.id,
                    knowledge_point_id=kp.id, task_type=models.TaskType.REVIEW.value,
                    task_date=task_date, duration_minutes=_REVIEW_MINUTES,
                    question_ids=[],
                )
                db.add(task)
                created.append(task)
                remaining_minutes -= _REVIEW_MINUTES
                review_count += 1

        # ===== ① 学习任务：按剩余天数均分（不再被当日剩余时长截断） =====
        if not is_last:
            unlearned = [
                kp for kp in ordered_kps
                if kp.id not in learn_dates and kp.id not in today_ids
            ]
            # 剩多少天还能排新学（最后一天不排）
            remaining_learn_days = max(1, plan.duration_days - day_index)
            new_quota = math.ceil(len(unlearned) / remaining_learn_days) if unlearned else 0
            # 保证每天至少推进 1 个新知识点，避免复习挤占后知识点在计划周期内被漏排
            if unlearned and new_quota < 1:
                new_quota = 1
            for kp in unlearned[:new_quota]:
                if (kp.id, models.TaskType.LEARN.value) in existing:
                    continue
                task = models.PlanTask(
                    user_id=plan.user_id, plan_id=plan.id,
                    knowledge_point_id=kp.id, task_type=models.TaskType.LEARN.value,
                    task_date=task_date, duration_minutes=_LEARN_MINUTES,
                    question_ids=[],
                )
                db.add(task)
                created.append(task)
                learn_dates[kp.id] = task_date

        db.commit()
        return created

    # ===== 2. 每日回流（跨日推进计划）=====

    def _kp_importance(self, db: Session, plan: models.LearningPlan, kp_ids: set[int]) -> dict[int, int]:
        """知识点重要度（1~5）：优先取缓存，缺失时尽力调一次 LLM，失败回退 3。"""
        if not kp_ids:
            return {}
        from smart.question.service import _IMPORTANCE_CACHE
        kps = list(db.execute(
            select(models.KnowledgePoint).where(models.KnowledgePoint.id.in_(kp_ids))
        ).scalars().all())
        subject = (plan.subject or "").strip()
        out: dict[int, int] = {}
        todo: list[tuple[int, str]] = []
        for kp in kps:
            cached = _IMPORTANCE_CACHE.get((subject, kp.name)) if subject else None
            if cached:
                out[kp.id] = int(cached.get("importance", 3))
            else:
                out[kp.id] = 3
                todo.append((kp.id, kp.name))
        if subject and todo:
            try:
                got = get_ai_provider().rank_kp_importance(
                    subject, [{"name": n} for _, n in todo]
                ) or {}
            except Exception:
                got = {}
            for kid, name in todo:
                v = got.get(name) or {"importance": 3}
                out[kid] = int(v.get("importance", 3))
                _IMPORTANCE_CACHE[(subject, name)] = {
                    "importance": out[kid], "reason": v.get("reason", ""), "_assessed": True,
                }
        return out

    def _day_capacity_load(self, db: Session, plan_id: int, from_day: dt.date, days: int) -> dict[dt.date, int]:
        """from_day 起连续 days 天中，每天已被未完成任务占用的时长。"""
        rows = db.execute(
            select(models.PlanTask.task_date, func.sum(models.PlanTask.duration_minutes))
            .where(
                models.PlanTask.plan_id == plan_id,
                models.PlanTask.task_date >= from_day,
                models.PlanTask.task_date < from_day + dt.timedelta(days=days),
                models.PlanTask.status == models.TaskStatus.TODO.value,
            )
            .group_by(models.PlanTask.task_date)
        ).all()
        return {_as_date(d): int(m or 0) for d, m in rows}

    def _place_task_date(
        self, load: dict[dt.date, int], start: dt.date,
        duration: int, capacity: int, max_days: int,
    ) -> dt.date:
        """为任务找容身之日：优先放第一个装得下的日子；都装不下则放当前最空的一天。"""
        min_load_day, min_load = None, None
        for offset in range(max_days):
            day = start + dt.timedelta(days=offset)
            cur = load.get(day, 0)
            if min_load is None or cur < min_load:
                min_load_day, min_load = day, cur
            if cur + duration <= max(capacity, duration):
                load[day] = cur + duration
                return day
        # 全部超载：放到最空的一天（尽量平均）
        load[min_load_day] = min_load + duration
        return min_load_day

    def rollover_expired_tasks(
        self, db: Session, today: dt.date, max_carry_days: int = 7
    ) -> dict:
        """过期未完成任务：按重要程度平均重排到之后几天。

        - 原日期的任务保留并标记为 CARRIED（历史记录不丢，页面据此显示「当天未进行学习」）
        - 副本按重要度从高到低、结合每日时长容量铺到今天起的 max_carry_days 天内
        - 超期超过 max_carry_days 天的仍然置为 SKIPPED
        """
        overdue = list(db.execute(
            select(models.PlanTask).where(
                models.PlanTask.task_date < today,
                models.PlanTask.status == models.TaskStatus.TODO.value,
            ).order_by(models.PlanTask.task_date, models.PlanTask.id)
        ).scalars().all())
        if not overdue:
            return {"carried": 0, "skipped": 0, "copied": 0, "spread_days": 0}

        carried = skipped = copied = 0
        spread_days = 0
        by_plan: dict[int, list[models.PlanTask]] = {}
        for t in overdue:
            by_plan.setdefault(t.plan_id, []).append(t)

        for plan_id, tasks in by_plan.items():
            plan = db.get(models.LearningPlan, plan_id)
            if plan is None:
                continue
            fresh: list[models.PlanTask] = []
            for t in tasks:
                if (today - _as_date(t.task_date)).days > max_carry_days:
                    t.status = models.TaskStatus.SKIPPED.value
                    skipped += 1
                else:
                    fresh.append(t)
            if not fresh:
                continue

            imp = self._kp_importance(db, plan, {t.knowledge_point_id for t in fresh})
            # 重要度高的优先排到近的日子；同重要度保持原有日期/创建顺序
            fresh.sort(key=lambda t: (-imp.get(t.knowledge_point_id, 3),
                                      _as_date(t.task_date), t.id))

            load = self._day_capacity_load(db, plan.id, today, max_carry_days)
            for t in fresh:
                t.status = models.TaskStatus.CARRIED.value  # 原日期保留记录
                target = self._place_task_date(
                    load, today, t.duration_minutes or 15,
                    plan.daily_minutes, max_carry_days,
                )
                offset = (target - today).days + 1
                spread_days = max(spread_days, offset)
                db.add(models.PlanTask(
                    user_id=t.user_id, plan_id=t.plan_id,
                    knowledge_point_id=t.knowledge_point_id,
                    task_type=t.task_type, task_date=target,
                    duration_minutes=t.duration_minutes,
                    status=models.TaskStatus.TODO.value,
                    question_ids=t.question_ids,
                ))
                copied += 1
                carried += 1
        db.commit()
        return {"carried": carried, "skipped": skipped,
                "copied": copied, "spread_days": spread_days}

    def reset_plan(self, db: Session, user_id: int, plan_id: int) -> dict:
        """以今天为起点重置计划：所有未完成任务重新按容量均匀排期。

        - 已完成/已跳过的任务不动（历史保留）
        - 原未完成任务标记 CARRIED（保留记录），副本从今天起按重要度+容量重排
        """
        plan = db.execute(
            select(models.LearningPlan).where(
                models.LearningPlan.id == plan_id,
                models.LearningPlan.user_id == user_id,
            )
        ).scalars().first()
        if plan is None:
            raise ValueError("计划不存在")

        today = dt.date.today()
        tasks = list(db.execute(
            select(models.PlanTask).where(
                models.PlanTask.plan_id == plan_id,
                models.PlanTask.status == models.TaskStatus.TODO.value,
            ).order_by(models.PlanTask.task_date, models.PlanTask.id)
        ).scalars().all())
        if not tasks:
            return {"reset": True, "rescheduled": 0, "days": 0}

        imp = self._kp_importance(db, plan, {t.knowledge_point_id for t in tasks})
        tasks.sort(key=lambda t: (-imp.get(t.knowledge_point_id, 3),
                                  _as_date(t.task_date), t.id))

        load: dict[dt.date, int] = {}
        days_used: set[dt.date] = set()
        horizon = max(plan.duration_days or 14, 14) + 30  # 兜底范围足够宽
        rescheduled = 0
        for t in tasks:
            t.status = models.TaskStatus.CARRIED.value
            target = self._place_task_date(
                load, today, t.duration_minutes or 15, plan.daily_minutes, horizon,
            )
            db.add(models.PlanTask(
                user_id=t.user_id, plan_id=t.plan_id,
                knowledge_point_id=t.knowledge_point_id,
                task_type=t.task_type, task_date=target,
                duration_minutes=t.duration_minutes,
                status=models.TaskStatus.TODO.value,
                question_ids=t.question_ids,
            ))
            days_used.add(target)
            rescheduled += 1
        db.commit()
        return {"reset": True, "rescheduled": rescheduled, "days": len(days_used)}

    def run_daily_rollover(self, db: Session, today: dt.date | None = None) -> dict:
        """每日定时回流入库：顺延过期任务 + 为所有进行中的计划补齐当日任务。

        过期判定始终对标真实今天，today 参数只决定"为哪一天补排任务"，
        因此可用于补跑历史日期而不误伤现有未完成任务。
        """
        target = today or dt.date.today()
        expired = self.rollover_expired_tasks(db, dt.date.today())

        plans = list(db.execute(
            select(models.LearningPlan).where(models.LearningPlan.status == 1)
        ).scalars().all())

        created = 0
        for plan in plans:
            created += len(self.generate_day_tasks(db, plan, target))

        return {
            "date": target.isoformat(),
            "plans": len(plans),
            "created_tasks": created,
            **expired,
        }

    # ===== 3. 今日待办 =====

    def today_tasks(
        self, db: Session, user_id: int, plan_id: int | None = None
    ) -> list[models.PlanTask]:
        today = dt.date.today()
        stmt = select(models.PlanTask).where(
            models.PlanTask.user_id == user_id,
            models.PlanTask.task_date == today,
        )
        if plan_id is not None:
            stmt = stmt.where(models.PlanTask.plan_id == plan_id)
        return list(db.execute(stmt).scalars().all())

    # ===== 4. 回流：任务完成 → 更新 mastery + 遗忘曲线 → 调整排期 =====

    def mark_task_done(
        self,
        db: Session,
        task_id: int,
        is_correct: bool | None = None,
        actual_duration: int | None = None,
    ) -> models.PlanTask:
        task = db.get(models.PlanTask, task_id)
        if task is None:
            raise ValueError(f"任务 {task_id} 不存在")

        task.status = models.TaskStatus.DONE.value
        if actual_duration is not None:
            task.actual_duration = actual_duration

        # 有判分结果时回流掌握度与遗忘曲线（学习/复习/练习任务都适用）
        if is_correct is not None and task.task_type in (
            models.TaskType.LEARN.value,
            models.TaskType.REVIEW.value,
            models.TaskType.PRACTICE.value,
        ):
            kp = db.get(models.KnowledgePoint, task.knowledge_point_id)
            if kp is not None:
                self._update_mastery_and_schedule(db, kp, is_correct)

        db.commit()
        return task

    def mark_task_skipped(self, db: Session, task_id: int) -> models.PlanTask:
        task = db.get(models.PlanTask, task_id)
        if task is None:
            raise ValueError(f"任务 {task_id} 不存在")
        task.status = models.TaskStatus.SKIPPED.value
        db.commit()
        return task

    # ===== 5. mastery + 遗忘曲线更新（复刻 DeepTutor scheduler）=====

    def _update_mastery_and_schedule(
        self, db: Session, kp: models.KnowledgePoint, is_correct: bool
    ) -> None:
        """答完一题后：更新掌握度 + 重算下次复习时间。"""
        # 取最近答题对错历史
        stmt = select(models.AttemptRecord.is_correct).where(
            models.AttemptRecord.knowledge_point_id == kp.id
        ).order_by(models.AttemptRecord.created_at.desc()).limit(10)
        history = [r for r in db.execute(stmt).scalars().all()]
        history.insert(0, is_correct)
        kp.mastery = round(compute_mastery(list(reversed(history))), 2) * 100
        kp.mastery_count += 1

        # 新增答题记录
        record = models.AttemptRecord(
            user_id=kp.user_id, knowledge_point_id=kp.id,
            is_correct=is_correct,
        )
        db.add(record)

        self._advance_schedule(db, kp, is_correct)

    def _advance_schedule(
        self, db: Session, kp: models.KnowledgePoint, is_correct: bool
    ) -> None:
        """按对错推进遗忘曲线间隔并重算下一次复习时间。"""
        # 遗忘曲线 —— 复刻 DeepTutor SpacedRepetitionScheduler.schedule_next
        intervals = _INTERVAL_DAYS.get(kp.knowledge_type, _INTERVAL_DAYS["concept"])
        max_idx = len(intervals) - 1
        if is_correct:
            kp.consecutive_wrong = 0
            kp.consecutive_correct += 1
            if kp.consecutive_correct >= 2:
                kp.interval_index = min(kp.interval_index + 2, max_idx)
                kp.consecutive_correct = 0
            else:
                kp.interval_index = min(kp.interval_index + 1, max_idx)
        else:
            kp.consecutive_wrong += 1
            kp.consecutive_correct = 0
            kp.interval_index = max(kp.interval_index - 1, 0)
            if kp.consecutive_wrong >= 2:
                kp.consecutive_wrong = 0

        days = intervals[kp.interval_index]
        kp.last_review_time = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        kp.next_review_time = kp.last_review_time + dt.timedelta(days=days)
        db.flush()

    # ===== 6. 练习任务结算（逐题作答后一次性回流）=====

    def apply_practice_result(
        self,
        db: Session,
        task: models.PlanTask,
        correctness: list[bool],
        actual_minutes: int | None = None,
    ) -> models.PlanTask:
        """练习任务结算：按本次全部答题重算 mastery，并推进遗忘曲线。"""
        if not correctness:
            raise ValueError("练习任务尚无答题记录，无法结算")

        task.status = models.TaskStatus.DONE.value
        if actual_minutes:
            task.actual_duration = actual_minutes

        kp = db.get(models.KnowledgePoint, task.knowledge_point_id)
        if kp is not None:
            kp.mastery = round(compute_mastery(correctness), 2) * 100
            kp.mastery_count += len(correctness)
            self._advance_schedule(db, kp, sum(correctness) / len(correctness) >= 0.6)

        db.commit()
        db.refresh(task)
        return task


# ============ AttemptRecord（答题记录表，补进 models.py 里声明的）============

# 这张表要手动 import 触发注册（确保 init_db 时会建表）
from smart.models import AttemptRecord  # noqa: E402
