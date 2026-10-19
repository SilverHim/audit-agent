from fastapi import APIRouter

from backend.api.routes import (
    auth,
    clients,
    conversation,
    data,
    datasets,
    deferrals,
    engagements,
    evidence,
    gates,
    health,
    integrations,
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
    clients,
    engagements,
    gates,
    reviews,
    deferrals,
    conversation,
    evidence,
    data,
    reports,
    standards,
    settings,
    integrations,
):
    api_router.include_router(module.router)
api_router.include_router(data.schemas_router)
