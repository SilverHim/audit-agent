"""JWT, 口令哈希, 以及设置页所存密钥的加解密."""

from datetime import UTC, datetime, timedelta

import jwt
from cryptography.fernet import Fernet, InvalidToken
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


class SecretKeyError(Exception):
    """加解密设置页所存密钥时主密钥 SETTINGS_SECRET_KEY 不可用. code: SETTINGS_KEY_MISSING (没配) 或
    SETTINGS_KEY_INVALID (格式不对, 或与加密时用的不是同一个). 消息不含任何密钥."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _fernet(key: str) -> Fernet:
    if not key:
        raise SecretKeyError("SETTINGS_KEY_MISSING", "未配置 SETTINGS_SECRET_KEY, 不能保存或读取模型密钥")
    try:
        return Fernet(key)
    except ValueError:
        raise SecretKeyError("SETTINGS_KEY_INVALID", "SETTINGS_SECRET_KEY 不是有效的 Fernet 密钥") from None


def encrypt_secret(plain: str, key: str) -> str:
    """用主密钥 key (配置 SETTINGS_SECRET_KEY) 加密, 返回可存库的文本."""
    return _fernet(key).encrypt(plain.encode()).decode()


def decrypt_secret(token: str, key: str) -> str:
    """解开 encrypt_secret 的结果. 主密钥换过或密文被改时抛 SecretKeyError (SETTINGS_KEY_INVALID)."""
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken:
        raise SecretKeyError(
            "SETTINGS_KEY_INVALID", "已存的模型密钥无法用当前 SETTINGS_SECRET_KEY 解开, 请在设置页重填"
        ) from None
