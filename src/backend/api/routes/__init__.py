from fastapi import APIRouter

from backend.api.routes import auth, engagements, evidence, gates, health, reports, reviews, standards, users

api_router = APIRouter()
for module in (health, auth, users, engagements, gates, reviews, evidence, reports, standards):
    api_router.include_router(module.router)
