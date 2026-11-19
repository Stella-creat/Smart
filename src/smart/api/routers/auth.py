"""Auth 路由 — 注册 + 登录。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from smart.db import get_db
from smart.api.deps import get_current_user
from smart.api.routers import LoginReq, RegisterReq, Token
from smart.user.service import UserService
from smart import models

router = APIRouter()


@router.post("/register", response_model=dict)
def register(req: RegisterReq, db: Session = Depends(get_db)) -> dict:
    try:
        user = UserService.register(db, req.username, req.password, req.nickname)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/login", response_model=Token)
def login(req: LoginReq, db: Session = Depends(get_db)) -> Token:
    try:
        user, token = UserService.login(db, req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return Token(access_token=token)


@router.get("/me")
def me(user: models.User = Depends(get_current_user)) -> dict:
    return {"id": user.id, "username": user.username, "nickname": user.nickname, "role": user.role}
