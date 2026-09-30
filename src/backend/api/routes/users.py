"""GET /users/me."""

from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])
