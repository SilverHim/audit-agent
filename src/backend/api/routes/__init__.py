from fastapi import APIRouter

from backend.api.routes import (
    auth,
    datasets,
    deferrals,
    engagements,
    evidence,
    gates,
    health,
    reports,
    reviews,
    settings,
    standards,
    users,
)

api_router = APIRouter()
for module in (
    health,
    auth,
    users,
    datasets,
    engagements,
    gates,
    reviews,
    deferrals,
    evidence,
    reports,
    standards,
    settings,
):
    api_router.include_router(module.router)
