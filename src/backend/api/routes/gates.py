"""GET /engagements/{eid}/gate; POST /engagements/{eid}/gate-decisions."""

from fastapi import APIRouter

router = APIRouter(prefix="/engagements/{eid}", tags=["gates"])
