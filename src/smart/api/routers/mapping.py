"""Mapping 路由 — 手动触发四级匹配 + 人工审核队列。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from smart.db import get_db
from smart.api.deps import get_current_user
from smart import models
from smart.db_utils import to_dict, to_dict_list
from smart.adapter.container import get_ai_provider
from smart.adapter.matcher.pipeline import run_matching_pipeline, upsert_alias
from smart.knowledge.service import KnowledgeService

router = APIRouter()


@router.post("/sync")
def run_sync(
    kb_name: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """手动触发：从 DeepTutor 拉 chunk → 四级匹配 → 写暂存 + 正式表。"""
    provider = get_ai_provider()
    ks = KnowledgeService(provider)
    svc = KnowledgeService(provider)
    kps = svc.list_knowledge_points(db, user.id)
    chunks = ks.pull_chunks_for_matching(kb_name, top_k=50)
    counters = run_matching_pipeline(db, provider, chunks, kps)
    return {"chunks_pulled": len(chunks), "matching_counters": counters}


@router.get("/review-queue")
def review_queue(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    """人工审核队列 —— L3 相似度 0.6~0.85 区间的待处理映射。"""
    from sqlalchemy import select
    stmt = select(models.MappingStaging).where(models.MappingStaging.status == 0)
    rows = db.execute(stmt).scalars().all()
    return to_dict_list(rows)


@router.post("/review/{staging_id}/approve")
def approve_review(
    staging_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """审核通过 —— 写正式表 + 回写别名表让下次 L2 能命中。"""
    staging = db.get(models.MappingStaging, staging_id)
    if staging is None:
        return {"error": "暂存记录不存在"}
    staging.status = 1
    existing = db.query(models.MappingChunkPoint).filter_by(chunk_ref=staging.source_ref).one_or_none()
    if existing:
        existing.kp_id = staging.kp_id
        existing.confidence = staging.confidence
    else:
        db.add(models.MappingChunkPoint(
            chunk_ref=staging.source_ref, kp_id=staging.kp_id, confidence=staging.confidence,
        ))
    # 回写别名 —— 用 source_text 前 20 字当 alias
    if staging.source_text:
        upsert_alias(db, staging.source_text[:20], staging.kp_id, source="REVIEW_AUTO")
    db.commit()
    return {"status": "approved"}


@router.post("/review/{staging_id}/reject")
def reject_review(
    staging_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    staging = db.get(models.MappingStaging, staging_id)
    if staging is None:
        return {"error": "暂存记录不存在"}
    staging.status = 2
    db.commit()
    return {"status": "rejected"}
