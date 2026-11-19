"""公共依赖 — 鉴权等跨路由共享的 Depends。"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from smart import models
from smart.db import get_db
from smart.user.service import UserService

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> models.User:
    try:
        payload = UserService.decode_token(token)
        user_id = int(payload.get("sub", "0"))
    except (JWTError, ValueError):
        logger.warning("鉴权 401：Token 无效（token 前缀=%s…，长度=%s）", token[:12], len(token or ""))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")
    user = UserService.get_by_id(db, user_id)
    if user is None:
        logger.warning("鉴权 401：用户不存在（sub=%s）", user_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user
