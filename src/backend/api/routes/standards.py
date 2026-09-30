"""GET /standards/{fragment_id}."""

from fastapi import APIRouter

router = APIRouter(prefix="/standards", tags=["standards"])
