"""FastAPI 入口 — 组装各业务模块路由。"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from smart.config import settings
from smart.db import init_db
from smart.api.deps import oauth2_scheme  # noqa: F401 — 保留符号兼容
from smart.api.routers import auth, knowledge, question, plan, practice, mapping, deeptutor_proxy, feedback, visualize


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="智能知识库与个性化学习管理系统 — 业务层（DeepTutor 适配壳）",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== 注册路由 =====
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["knowledge"])
app.include_router(question.router, prefix="/api/question", tags=["question"])
app.include_router(plan.router, prefix="/api/plan", tags=["plan"])
app.include_router(practice.router, prefix="/api/practice", tags=["practice"])
app.include_router(mapping.router, prefix="/api/mapping", tags=["mapping"])
app.include_router(deeptutor_proxy.router, prefix="/api/deeptutor", tags=["deeptutor"])
app.include_router(feedback.router, prefix="/api/feedback", tags=["feedback"])
app.include_router(visualize.router, prefix="/api/visualize", tags=["visualize"])


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    if settings.daily_rollover_enabled:
        from smart.jobs.daily import daily_loop

        asyncio.create_task(daily_loop())


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name}


# ===== 静态前端页面 =====
from pathlib import Path

_WEB_DIR = Path(__file__).resolve().parents[3] / "web"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    # 开发期避免浏览器缓存旧页面导致前端改动不生效
    return FileResponse(
        _WEB_DIR / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


if _WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")


def run(host: str = "0.0.0.0", port: int = 8002) -> None:
    import uvicorn
    uvicorn.run("smart.api.main:app", host=host, port=port, reload=settings.debug)
