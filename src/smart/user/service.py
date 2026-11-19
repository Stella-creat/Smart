"""用户模块 — 注册登录 + JWT 鉴权。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from smart.config import settings
from smart import models


def _hash_pwd(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_pwd(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


class UserService:
    @staticmethod
    def register(db: Session, username: str, password: str, nickname: str | None = None) -> models.User:
        existing = db.execute(select(models.User).where(models.User.username == username)).scalar_one_or_none()
        if existing is not None:
            raise ValueError("用户名已存在")
        user = models.User(
            username=username, password_hash=_hash_pwd(password),
            nickname=nickname,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def login(db: Session, username: str, password: str) -> tuple[models.User, str]:
        user = db.execute(select(models.User).where(models.User.username == username)).scalar_one_or_none()
        if user is None or not _verify_pwd(password, user.password_hash):
            raise ValueError("用户名或密码错误")
        token = UserService.make_token(user.id)
        return user, token

    @staticmethod
    def make_token(user_id: int) -> str:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(minutes=settings.jwt_expire_minutes)
        return jwt.encode(
            {"sub": str(user_id), "iat": int(now.timestamp()), "exp": int(exp.timestamp())},
            settings.jwt_secret, algorithm=settings.jwt_algorithm,
        )

    @staticmethod
    def decode_token(token: str) -> dict[str, Any]:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])

    @staticmethod
    def get_by_id(db: Session, user_id: int) -> models.User | None:
        return db.get(models.User, user_id)
