"""POST /auth/login; GET /users/me 放 users 路由."""

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])
