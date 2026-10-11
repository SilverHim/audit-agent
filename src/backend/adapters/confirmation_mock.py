"""Durable, project-scoped idempotent synthetic confirmation channel."""

import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.contracts.ports import ConfirmationReceipt, ConfirmationRequest
from backend.core.config import get_settings
from backend.persistence.stores import safe_child


class MockConfirmationChannel:
    def __init__(self, dataset_dir: Path, receipt_root: Path | None = None) -> None:
        self.dataset_dir = dataset_dir
        self.receipt_root = receipt_root or get_settings().EVIDENCE_ROOT / ".confirmation_receipts"

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        directory = safe_child(self.receipt_root, eid)
        directory.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        path = directory / (hashlib.sha256(idempotency_key.encode()).hexdigest() + ".json")
        if not path.exists():
            reply_path = safe_child(self.dataset_dir / "confirmations", req.account_no + ".pdf")
            channels = json.loads((self.dataset_dir / "confirmations" / "receipts.json").read_text())
            sent_at = datetime.now(UTC)
            value = dict(
                fingerprint=fingerprint,
                sent_at=sent_at.isoformat(),
                replied_at=sent_at.astimezone(ZoneInfo("Australia/Sydney")).date().isoformat()
                if reply_path.exists()
                else None,
                reply_file_name=reply_path.name if reply_path.exists() else None,
                receipt_channel=channels.get(req.account_no, {}).get("receipt_channel")
                if reply_path.exists()
                else None,
                reply_hex=reply_path.read_bytes().hex() if reply_path.exists() else None,
            )
            # Publish a complete record atomically; competing processes use the first receipt.
            from uuid import uuid4

            temp = directory / f".{uuid4()}.tmp"
            temp.write_text(json.dumps(value), encoding="utf-8")
            try:
                with suppress(FileExistsError):
                    path.hardlink_to(temp)
            finally:
                temp.unlink()
        saved = json.loads(path.read_text())
        if saved.pop("fingerprint") != fingerprint:
            raise ValueError("Idempotency key reused with a different confirmation request")
        reply_hex = saved.pop("reply_hex")
        return ConfirmationReceipt.model_validate(
            dict(**saved, reply_bytes=bytes.fromhex(reply_hex) if reply_hex is not None else None)
        )
