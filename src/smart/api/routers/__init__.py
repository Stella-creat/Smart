"""公共依赖 + Pydantic schemas。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


# ===== Pydantic schemas =====


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterReq(BaseModel):
    username: str
    password: str
    nickname: str | None = None


class LoginReq(BaseModel):
    username: str
    password: str
