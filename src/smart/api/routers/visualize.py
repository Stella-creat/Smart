"""Visualize 路由 —— 给知识点生成动画讲解。"""
from __future__ import annotations

import time
import traceback
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from smart import models
from smart.adapter.container import get_ai_provider
from smart.api.deps import get_current_user
from smart.db import get_db

router = APIRouter()

# 进程内缓存：kp_id -> {"result": dict, "ts": float}
# 命中后不再调 DeepTutor WS，TTL 3 小时
_CACHE: dict[int, dict[str, Any]] = {}
_CACHE_TTL = 3 * 3600  # 3 小时


def _get_cached(kp_id: int) -> dict | None:
    entry = _CACHE.get(kp_id)
    if not entry:
        return None
    if time.time() - entry["ts"] > _CACHE_TTL:
        _CACHE.pop(kp_id, None)
        return None
    return entry["result"]


def _set_cached(kp_id: int, result: dict) -> None:
    _CACHE[kp_id] = {"result": result, "ts": time.time()}


@router.get("/kp/{kp_id}")
def visualize_kp(
    kp_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """返回知识点的讲解文字 + SVG/HTML 动画内容（进程内缓存 3 小时）。"""
    # 1. 先查缓存
    cached = _get_cached(kp_id)
    if cached is not None:
        resp = {"cached": True, "cache_ttl": int(_CACHE_TTL - (time.time() - _CACHE[kp_id]["ts"]))}
        resp.update(cached)
        return resp

    # 2. 缓存未命中 → 查知识点 + 调 AI
    kp = db.get(models.KnowledgePoint, kp_id)
    if kp is None:
        raise HTTPException(status_code=404, detail=f"知识点 {kp_id} 不存在")
    if kp.user_id not in (0, user.id):
        raise HTTPException(status_code=403, detail="无权访问此知识点")

    try:
        result = get_ai_provider().visualize_kp(
            kp_name=kp.name,
            kp_content=kp.content or "",
            kp_type=str(kp.knowledge_type or "concept"),
            subject=kp.subject or "",
        )
        full = {
            "kp_id": kp.id,
            "kp_name": kp.name,
            "subject": kp.subject,
            **result,
        }
        _set_cached(kp_id, full)
        return {"cached": False, **full}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"动画生成失败: {type(e).__name__}: {e}")


@router.delete("/kp/{kp_id}/cache")
def clear_kp_cache(kp_id: int) -> dict:
    """手动清除某个知识点的缓存（调试用）。"""
    _CACHE.pop(kp_id, None)
    return {"ok": True, "cleared": True}


@router.delete("/cache")
def clear_all_cache() -> dict:
    """清除所有动画缓存。"""
    n = len(_CACHE)
    _CACHE.clear()
    return {"ok": True, "cleared_count": n}
