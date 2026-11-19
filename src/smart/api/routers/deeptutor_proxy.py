"""DeepTutor Proxy 路由 — 直接代理到 DeepTutor 的 REST 端点（只读）。

方便前端调试 DeepTutor 原始数据，不走适配层。
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException

from smart.api.deps import get_current_user
from smart.config import settings
from smart import models

router = APIRouter()


@router.get("/{kb_name}/raw")
def deep_proxy_docs(kb_name: str, user: models.User = Depends(get_current_user)) -> list[dict]:
    """代理 DeepTutor 的原始文档列表。"""
    try:
        r = httpx.get(
            f"{settings.deeptutor_base_url}/api/knowledge-bases/{kb_name}/files",
            timeout=settings.deeptutor_timeout,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"DeepTutor 不可用: {e}")


@router.get("/kbs")
def deep_proxy_kbs(user: models.User = Depends(get_current_user)) -> list:
    try:
        r = httpx.get(
            f"{settings.deeptutor_base_url}/api/knowledge-bases/configs",
            timeout=settings.deeptutor_timeout,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            kbs = data.get("knowledge_bases", {})
            return [{"name": n, "status": c.get("status", "")} for n, c in kbs.items()]
        return data
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"DeepTutor 不可用: {e}")
