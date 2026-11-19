"""Feedback 路由 — 客服 / 意见反馈工单。"""
from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from smart.db import get_db
from smart.api.deps import get_current_user
from smart import models
from smart.db_utils import to_dict_list

router = APIRouter()

CATEGORIES = {"bug", "content", "usage", "suggestion", "other"}


class FeedbackCreate(BaseModel):
    category: str = "other"
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    contact: str | None = None


@router.post("")
def create_feedback(
    req: FeedbackCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """提交一条反馈（工单）。"""
    category = req.category if req.category in CATEGORIES else "other"
    row = models.Feedback(
        user_id=user.id,
        category=category,
        title=req.title.strip(),
        content=req.content.strip(),
        contact=(req.contact or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": row.status}


@router.get("/mine")
def list_my_feedback(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    """我的反馈记录（按时间倒序）。"""
    rows = db.execute(
        select(models.Feedback)
        .where(models.Feedback.user_id == user.id)
        .order_by(models.Feedback.id.desc())
    ).scalars().all()
    return to_dict_list(rows)


@router.delete("/{feedback_id}")
def delete_feedback(
    feedback_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """撤回自己提交且尚未处理的反馈。"""
    row = db.get(models.Feedback, feedback_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="反馈不存在")
    if row.status != models.FeedbackStatus.OPEN.value:
        raise HTTPException(status_code=400, detail="已受理的反馈无法撤回")
    db.delete(row)
    db.commit()
    return {"deleted": True}
