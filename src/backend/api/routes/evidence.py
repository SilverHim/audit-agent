"""GET documents、contracts、evidence/{document_id}、records/{source_type}/{source_id}、confirmations。"""

from fastapi import APIRouter

router = APIRouter(prefix="/engagements/{eid}", tags=["evidence"])
