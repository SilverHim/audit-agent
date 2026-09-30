"""JWT 与口令哈希 (C 负责)."""

from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from backend.core.config import get_settings

ALGORITHM = "HS256"
password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def create_access_token(user_id: str) -> str:
    settings = get_settings()
    expire = datetime.now(UTC) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str:
    """返回用户 ID; 过期或签名不符抛 jwt.PyJWTError."""
    payload = jwt.decode(token, get_settings().JWT_SECRET, algorithms=[ALGORITHM])
    return str(payload["sub"])
