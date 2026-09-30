"""函证平台 mock (B 负责): 发函即返回数据集目录下 confirmations/{account_no}.pdf, 同一幂等键返回同一回执."""

from pathlib import Path

from backend.contracts.ports import ConfirmationReceipt, ConfirmationRequest


class MockConfirmationChannel:
    """函证平台的 mock: 发函即返回数据集里预置的回函 PDF."""

    def __init__(self, dataset_dir: Path) -> None:
        self.dataset_dir = dataset_dir  # 数据集目录

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        raise NotImplementedError
