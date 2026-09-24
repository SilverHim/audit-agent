"""POST、GET /engagements；GET /engagements/{eid}；POST /engagements/{eid}/resume。"""

from fastapi import APIRouter

router = APIRouter(prefix="/engagements", tags=["engagements"])
