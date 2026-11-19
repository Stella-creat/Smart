"""Plan 路由 — 学习计划生成 + 今日待办 + 回流。"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smart.db import get_db
from smart.api.deps import get_current_user
from smart import models
from smart.db_utils import to_dict, to_dict_list
from smart.plan.service import PlanService, _as_date
from smart.knowledge.service import KnowledgeService
from smart.adapter.container import get_ai_provider

router = APIRouter()


class PlanCreate(BaseModel):
    subject: str
    exam_date: str | None = None
    daily_minutes: int = 60
    duration_days: int = 14


class TaskComplete(BaseModel):
    is_correct: bool | None = None
    actual_duration: int | None = None


@router.post("")
def create_plan(
    req: PlanCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    svc = PlanService()
    # 计划的「科目」现在来自知识库选择；该科目下没有知识点时补占位点，保证能排出任务
    KnowledgeService(get_ai_provider()).ensure_subject_kp(db, user.id, req.subject)
    exam = dt.date.fromisoformat(req.exam_date) if req.exam_date else None
    days = max(1, min(int(req.duration_days or 14), 120))
    try:
        plan = svc.create_plan(db, user.id, req.subject, exam, req.daily_minutes, days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return to_dict(plan)


@router.delete("/plans/{plan_id}")
def delete_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """删除学习计划及其全部任务，删除后该知识库可重新建计划。"""
    svc = PlanService()
    if not svc.delete_plan(db, user.id, plan_id):
        raise HTTPException(status_code=404, detail="计划不存在")
    return {"deleted": True, "plan_id": plan_id}


@router.get("/plans")
def list_plans(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    """我的学习计划列表（按创建时间倒序，附带任务数）。"""
    rows = db.execute(
        select(models.LearningPlan)
        .where(models.LearningPlan.user_id == user.id)
        .order_by(models.LearningPlan.id.desc())
    ).scalars().all()
    counts = dict(db.execute(
        select(models.PlanTask.plan_id, func.count(models.PlanTask.id))
        .where(
            models.PlanTask.user_id == user.id,
            models.PlanTask.status != models.TaskStatus.CARRIED.value,
        )
        .group_by(models.PlanTask.plan_id)
    ).all())
    out = []
    for p in rows:
        item = to_dict(p)
        item["task_count"] = int(counts.get(p.id, 0))
        out.append(item)
    return out


@router.get("/plans/{plan_id}/days")
def plan_days(
    plan_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """按天分组返回计划任务（每天一个模块，从计划首日开始编号）。"""
    plan = db.get(models.LearningPlan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=404, detail="计划不存在")

    tasks = list(db.execute(
        select(models.PlanTask)
        .where(models.PlanTask.plan_id == plan_id)
        .order_by(models.PlanTask.task_date, models.PlanTask.id)
    ).scalars().all())
    # 已顺延（CARRIED）且日期在今天之后的记录是被重排替代的旧排期，不返回，
    # 避免同一天出现重复任务；过去日期的 CARRIED 保留，用于展示当天排期历史。
    _today = dt.date.today()
    tasks = [
        t for t in tasks
        if not (int(t.status) == int(models.TaskStatus.CARRIED.value)
                and _as_date(t.task_date) >= _today)
    ]

    kp_ids = {t.knowledge_point_id for t in tasks}
    kp_rows = {
        kp.id: kp for kp in db.execute(
            select(models.KnowledgePoint).where(models.KnowledgePoint.id.in_(kp_ids))
        ).scalars().all()
    } if kp_ids else {}

    # 知识点重要度评估（从 question.service 复用 _IMPORTANCE_CACHE）
    from smart.question.service import _IMPORTANCE_CACHE
    from smart.adapter.container import get_ai_provider
    kp_importance: dict[str, dict] = {}
    subject = (plan.subject or "").strip()
    if subject and kp_rows:
        # 先从缓存里取
        all_names = [kp_rows[kid].name for kid in kp_ids if kid in kp_rows]
        for n in all_names:
            cached = _IMPORTANCE_CACHE.get((subject, n))
            if cached:
                kp_importance[n] = cached
            else:
                kp_importance[n] = {"importance": 3, "reason": ""}
        # 还有没评估过的就调一次 LLM（带缓存，同一进程后续不会重复调）
        todo = [n for n, v in kp_importance.items() if v["importance"] == 3 and not v.get("_assessed")]
        # 避免占位知识点误导模型（name 等于 subject 的跳过）
        todo = [n for n in todo if n != subject and n.strip()]
        if todo:
            try:
                got = get_ai_provider().rank_kp_importance(
                    subject, [{"name": n} for n in todo]
                ) or {}
            except Exception:
                got = {}
            for n in todo:
                kp_importance[n] = got.get(n, {"importance": 3, "reason": ""})
                _IMPORTANCE_CACHE[(subject, n)] = kp_importance[n]
                kp_importance[n]["_assessed"] = True
        # 占位知识点给最低分
        for n in all_names:
            if n == subject:
                kp_importance[n] = {"importance": 1, "reason": "占位知识点", "_assessed": True}

    days: list[dict] = []
    if tasks:
        start = min(_as_date(t.task_date) for t in tasks)
        last = max(_as_date(t.task_date) for t in tasks)
        span = (last - start).days + 1
        span = max(span, int(plan.duration_days or 0))
    else:
        start = dt.date.today()
        span = int(plan.duration_days or 0)
    if span:
        by_date: dict[dt.date, list[models.PlanTask]] = {}
        for t in tasks:
            by_date.setdefault(_as_date(t.task_date), []).append(t)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for offset in range(min(span, 120)):
            day = start + dt.timedelta(days=offset)
            rows = by_date.get(day, [])
            day_tasks = []
            for t in rows:
                kp = kp_rows.get(t.knowledge_point_id)
                imp = kp_importance.get(kp.name if kp else "", {"importance": 3, "reason": ""}) if subject else {"importance": 3, "reason": ""}
                day_tasks.append({
                    "id": t.id,
                    "task_type": getattr(t.task_type, "value", t.task_type),
                    "status": int(t.status),
                    "duration_minutes": t.duration_minutes,
                    "actual_duration": t.actual_duration,
                    "knowledge_point_id": t.knowledge_point_id,
                    "kp_name": kp.name if kp else "",
                    "mastery": float(kp.mastery) if kp and kp.mastery is not None else 0.0,
                    "importance": int(imp.get("importance", 3)),
                    "importance_reason": imp.get("reason", "") or "",
                })
            days.append({
                "date": day.isoformat(),
                "day_index": offset + 1,
                "weekday": weekdays[day.weekday()],
                "is_today": day == dt.date.today(),
                "tasks": day_tasks,
            })

    return {"plan": to_dict(plan), "days": days}


@router.get("/today")
def today_tasks(
    plan_id: int | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    svc = PlanService()
    tasks = svc.today_tasks(db, user.id, plan_id)
    return to_dict_list(tasks)


@router.post("/rollover")
def rollover(
    target_date: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """手动触发每日回流（可传 target_date 补跑指定日期）。"""
    svc = PlanService()
    day = dt.date.fromisoformat(target_date) if target_date else None
    return svc.run_daily_rollover(db, day)


@router.post("/plans/{plan_id}/reset")
def reset_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """以今天为起点重置计划：未完成任务按重要度+每日容量重新均匀排期。"""
    svc = PlanService()
    try:
        return svc.reset_plan(db, user.id, plan_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/complete")
def complete_task(
    task_id: int,
    req: TaskComplete,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    svc = PlanService()
    task = svc.mark_task_done(db, task_id, req.is_correct, req.actual_duration)
    return to_dict(task)


@router.post("/tasks/{task_id}/skip")
def skip_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    svc = PlanService()
    task = svc.mark_task_skipped(db, task_id)
    return to_dict(task)
