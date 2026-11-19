"""Practice 路由 —— 练习组卷、逐题作答、任务结算。"""
from __future__ import annotations

import traceback
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from smart import models
from smart.adapter.container import get_ai_provider
from smart.api.deps import get_current_user
from smart.db import get_db
from smart.db_utils import to_dict
from smart.practice.service import PracticeService

router = APIRouter()


class SubmitReq(BaseModel):
    question_id: int
    user_answer: str
    duration_seconds: int | None = None


def _svc() -> PracticeService:
    return PracticeService(get_ai_provider())


@router.get("/tasks/{task_id}")
def get_paper(
    task_id: int,
    size: int = 3,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """获取练习试卷（自动配题，不下发答案，支持断点续练）。"""
    try:
        return _svc().paper(db, user.id, task_id, size)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        db.rollback()
        raise HTTPException(status_code=500, detail=f"practice error: {type(e).__name__}: {e}")


@router.post("/tasks/{task_id}/generate")
def generate_questions(
    task_id: int,
    count: int = 3,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """任务暂无题目时，调用 AI 为该知识点生成练习并挂到任务上。"""
    try:
        return _svc().force_generate(db, user.id, task_id, count)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        db.rollback()
        raise HTTPException(status_code=500, detail=f"generate error: {type(e).__name__}: {e}")


@router.post("/kp/{kp_id}/generate")
def generate_for_kp(
    kp_id: int,
    count: int = 3,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """知识点没有练习任务时：自动建今日任务并调用 AI 生成练习。"""
    try:
        return _svc().generate_for_kp(db, user.id, kp_id, count)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        db.rollback()
        raise HTTPException(status_code=500, detail=f"generate error: {type(e).__name__}: {e}")


@router.post("/tasks/{task_id}/submit")
def submit_answer(
    task_id: int,
    req: SubmitReq,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """提交单题，自动规则/AI 判分；全部答完自动结算任务。"""
    try:
        return _svc().submit(
            db, user.id, task_id, req.question_id, req.user_answer, req.duration_seconds
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/finish")
def finish_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """提前结束练习并结算：回流掌握度与遗忘曲线。"""
    try:
        task = _svc().finish(db, user.id, task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    kp = db.get(models.KnowledgePoint, task.knowledge_point_id)
    return {
        **to_dict(task),
        "kp_mastery": float(kp.mastery) if kp and kp.mastery is not None else 0.0,
        "kp_name": kp.name if kp else None,
    }


@router.get("/tasks/{task_id}/records")
def list_records(
    task_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    """本次练习的逐题作答记录（附带题干/选项/正确答案/解析，供成绩页展示）。"""
    try:
        rows = _svc().records(db, user.id, task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    qids = {r.get("question_id") for r in rows if r.get("question_id") is not None}
    questions: dict[int, models.Question] = {}
    if qids:
        for q in db.query(models.Question).filter(models.Question.id.in_(qids)):
            questions[q.id] = q
    out = []
    for r in rows:
        q = questions.get(r.get("question_id"))
        out.append({
            **r,
            "type": q.type if q else None,
            "difficulty": q.difficulty if q else None,
            "content": q.content if q else None,
            "options": q.options if q else None,
            "answer": q.answer if q else None,
            "explanation": q.explanation if q else None,
        })
    return out
