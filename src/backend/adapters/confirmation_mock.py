"""函证平台的 mock (B 负责): 回执按项目与幂等键持久化, 重启后同键返回同一回执; 回函读数据集目录下的
confirmations/."""

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.contracts.ports import ConfirmationReceipt, ConfirmationRequest, EngagementStore
from backend.core.config import get_settings
from backend.persistence.stores import safe_child


class MockConfirmationChannel:
    """回函 mock: 读 dataset_dir/confirmations/{账号}.pdf 与 receipts.json (回函渠道); dataset_dir 为 None, 或没有
    该账号的回函时出未回函回执. 回执按项目与幂等键持久化在 receipt_root 下, 重启后同键返回同一回执."""

    def __init__(self, dataset_dir: Path | None, receipt_root: Path | None = None) -> None:
        self.dataset_dir = dataset_dir
        self.receipt_root = receipt_root or get_settings().EVIDENCE_ROOT / ".confirmation_receipts"

    def receipt_path(self, eid: str, idempotency_key: str) -> Path:
        """回执文件: receipt_root/{eid}/sha256(幂等键).json."""
        return safe_child(self.receipt_root, eid) / (hashlib.sha256(idempotency_key.encode()).hexdigest() + ".json")

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        path = self.receipt_path(eid, idempotency_key)
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        if not path.exists():
            replies = self.dataset_dir / "confirmations" if self.dataset_dir is not None else None
            reply_path = safe_child(replies, req.account_no + ".pdf") if replies is not None else None
            replied = reply_path is not None and reply_path.exists()
            receipts = replies / "receipts.json" if replies is not None else None
            channels = json.loads(receipts.read_text()) if receipts is not None and replied else {}
            sent_at = datetime.now(UTC)
            value = dict(
                fingerprint=fingerprint,
                sent_at=sent_at.isoformat(),
                replied_at=sent_at.astimezone(ZoneInfo("Australia/Sydney")).date().isoformat() if replied else None,
                reply_file_name=reply_path.name if replied and reply_path else None,
                receipt_channel=channels.get(req.account_no, {}).get("receipt_channel") if replied else None,
                reply_hex=reply_path.read_bytes().hex() if replied and reply_path else None,
            )
            # 先写临时文件再以硬链接发布完整的回执; 并发时以先发布的为准
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


class DatasetConfirmationChannel:
    """按项目的数据集定位回函 mock: Numbat (demo) 读 data/demo/confirmations/, Xero 读补造目录的 confirmations/
    (补造与快照不匹配时抛 SupplementMismatch); 没有目录 (含没有数据集的新客户) 时出未回函回执. 回执持久化同
    MockConfirmationChannel.
    项目不存在抛 LookupError; 同键已有回执时直接取回, 不再解析目录 (已发出的函证不因补造或快照后来的变化而取不回)."""

    def __init__(
        self, engagements: EngagementStore, directory: Callable[[str], Path | None], receipt_root: Path | None = None
    ) -> None:
        self.engagements = engagements
        self.directory = directory
        self.receipt_root = receipt_root

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        engagement = await self.engagements.get(eid)
        if engagement is None:
            raise LookupError(f"没有项目 {eid}")
        stored = MockConfirmationChannel(None, self.receipt_root)
        if stored.receipt_path(eid, idempotency_key).exists():
            return await stored.submit(eid, req, idempotency_key)  # 已有回执: 不读数据集目录
        found = self.directory(engagement.dataset) if engagement.dataset is not None else None  # 新建的客户没有目录
        return await MockConfirmationChannel(found, self.receipt_root).submit(eid, req, idempotency_key)
