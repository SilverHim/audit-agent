from fastapi import APIRouter

from backend.api.routes import (
    assistant_actions,
    auth,
    clients,
    conversation,
    corrections,
    data,
    datasets,
    deferrals,
    engagements,
    evidence,
    execution,
    gates,
    health,
    integrations,
    manual,
    reports,
    reviews,
    settings,
    standards,
    users,
    workpapers,
)

api_router = APIRouter()
for module in (
    health,
    assistant_actions,
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
    execution,
    data,
    reports,
    corrections,
    standards,
    settings,
    integrations,
    workpapers,
):
    api_router.include_router(module.router)
api_router.include_router(data.schemas_router)
