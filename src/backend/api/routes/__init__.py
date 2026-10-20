from fastapi import APIRouter

from backend.api.routes import (
    auth,
    clients,
    conversation,
    corrections,
    data,
    datasets,
    deferrals,
    engagements,
    evidence,
    gates,
    health,
    integrations,
    manual,
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
    manual,
    deferrals,
    conversation,
    evidence,
    data,
    reports,
    corrections,
    standards,
    settings,
    integrations,
):
    api_router.include_router(module.router)
api_router.include_router(data.schemas_router)
