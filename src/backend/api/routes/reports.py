"""GET misstatements、reports、trail、trail/{trail_id}。"""

from fastapi import APIRouter

router = APIRouter(prefix="/engagements/{eid}", tags=["reports"])
