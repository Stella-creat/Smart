"""四级匹配引擎 — 把 DeepTutor chunk/文档 映射到知识点树。

调用顺序：L1 → L2 → L3；置信度 ≥ auto_threshold 直接入库，
介于 review_threshold 和 auto_threshold 之间进入人工审核队列。
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from smart.adapter.provider.ai_provider import AIProvider, Chunk
from smart.config import settings
from smart import models


# ============ L1 路径精确匹配 ============


def _normalize_path(s: str) -> str:
    return s.replace("\\", "/").strip().lower()


def match_exact(db: Session, chunk: Chunk, knowledge_points: list[models.KnowledgePoint]) -> tuple[int | None, float]:
    """路径精确匹配 —— 规范化后全等。

    匹配来源：chunk.source_doc 或 chunk.ref 中的路径片段，与知识点树的
    subject/chapter/name 拼接路径做精确比较。
    """
    chunk_path = _normalize_path(chunk.source_doc or chunk.ref)
    if not chunk_path:
        return None, 0.0
    for kp in knowledge_points:
        kp_path = _normalize_path(f"{kp.subject}/{kp.chapter}/{kp.name}")
        # 双向匹配：chunk 路径包含 kp 路径或反之
        if kp_path in chunk_path or chunk_path in kp_path:
            return kp.id, 1.0
    return None, 0.0


# ============ L2 别名表匹配 ============


def match_alias(db: Session, chunk: Chunk) -> tuple[int | None, float]:
    """查 t_mapping_alias —— 人工沉淀的别名 → kp_id。"""
    text = (chunk.text + " " + chunk.source_doc).lower()
    stmt = select(models.MappingAlias)
    for row in db.execute(stmt).scalars().all():
        if row.alias.lower() in text:
            return row.kp_id, 0.95
    return None, 0.0


# ============ L3 语义相似度（LLM 直接判断）============


def match_semantic(
    provider: AIProvider,
    chunk: Chunk,
    knowledge_points: list[models.KnowledgePoint],
    top_n: int = 3,
) -> list[tuple[int, float]]:
    """用 LLM 逐个判断语义相似度，取 top_n 候选。

    替代了原来的 embed() + cosine 实现 —— DeepSeek 官方无公开 embedding API，
    实测 LLM 语义判断（0.72-0.85 高相似 / 0.00 低相似）区分度更好。
    代价：每个 chunk × 每个 KP 一次 LLM 调用（≈0.5s/次），四级匹配场景可接受。
    """
    if not knowledge_points:
        return []
    chunk_text = chunk.text[:300]
    results: list[tuple[int, float]] = []
    for kp in knowledge_points:
        kp_text = f"{kp.subject} {kp.chapter} {kp.name}"
        sim = provider.semantic_similarity(chunk_text, kp_text)
        results.append((kp.id, sim))
    results.sort(key=lambda x: -x[1])
    return results[:top_n]


# ============ 主入口 ============


def run_matching_pipeline(
    db: Session,
    provider: AIProvider,
    chunks: Iterable[Chunk],
    knowledge_points: list[models.KnowledgePoint],
) -> dict[str, int]:
    """执行完整匹配流水线，写暂存表 + 正式映射表。

    返回各匹配级别的命中计数，方便日志和监控。
    """
    counters = {"L1_exact": 0, "L2_alias": 0, "L3_semantic_auto": 0, "L4_review": 0, "dropped": 0}

    for chunk in chunks:
        matched: tuple[int | None, float] = (None, 0.0)
        level = 0

        # L1
        kp_id, conf = match_exact(db, chunk, knowledge_points)
        if kp_id is not None:
            matched = (kp_id, conf)
            level = 1
            counters["L1_exact"] += 1
        else:
            # L2
            kp_id, conf = match_alias(db, chunk)
            if kp_id is not None:
                matched = (kp_id, conf)
                level = 2
                counters["L2_alias"] += 1
            else:
                # L3 — 取 top1
                top = match_semantic(provider, chunk, knowledge_points, top_n=1)
                if top:
                    kp_id, conf = top[0]
                    if conf >= settings.semantic_similarity_threshold_auto:
                        matched = (kp_id, conf)
                        level = 3
                        counters["L3_semantic_auto"] += 1
                    elif conf >= settings.semantic_similarity_threshold_review:
                        # 进入暂存表（待人工审核）
                        staging = models.MappingStaging(
                            source_type="DEEPTUTOR_CHUNK",
                            source_ref=chunk.ref,
                            source_text=chunk.text[:200],
                            kp_id=kp_id,
                            match_level=3,
                            confidence=conf,
                            status=0,
                        )
                        db.add(staging)
                        counters["L4_review"] += 1
                        continue
                    else:
                        counters["dropped"] += 1
                        continue
                else:
                    counters["dropped"] += 1
                    continue

        # L1/L2/L3_auto 全部通过 → 写暂存 + 正式表
        staging = models.MappingStaging(
            source_type="DEEPTUTOR_CHUNK",
            source_ref=chunk.ref,
            source_text=chunk.text[:200],
            kp_id=matched[0] or 0,
            match_level=level,
            confidence=matched[1],
            status=1,  # 已通过
        )
        db.add(staging)
        # 正式表：chunk_ref 唯一 — 已存在则更新
        existing = db.query(models.MappingChunkPoint).filter_by(chunk_ref=chunk.ref).one_or_none()
        if existing:
            existing.kp_id = matched[0] or existing.kp_id
            existing.confidence = matched[1]
        else:
            db.add(models.MappingChunkPoint(
                chunk_ref=chunk.ref,
                kp_id=matched[0] or 0,
                confidence=matched[1],
            ))

    db.commit()
    return counters


def upsert_alias(db: Session, alias: str, kp_id: int, source: str = "MANUAL") -> models.MappingAlias:
    """人工审核通过后，回写别名表 —— 让下次 L2 能自动命中。"""
    existing = db.query(models.MappingAlias).filter_by(alias=alias.lower()).one_or_none()
    if existing:
        existing.kp_id = kp_id
        return existing
    row = models.MappingAlias(alias=alias.lower(), kp_id=kp_id, source=source)
    db.add(row)
    db.commit()
    return row
