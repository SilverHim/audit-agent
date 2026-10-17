"""POST /auth/login, /auth/register."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from backend.agents.base import AgentDeps
from backend.api.deps import get_deps
from backend.api.errors import api_error
from backend.api.schemas import RegisterIn, Token
from backend.core.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

USERNAME_MAX = 254  # 账号最长, 与邮箱上限一致
FULL_NAME_MAX = 100  # 姓名最长
PASSWORD_RANGE = (8, 128)  # 密码长度下限与上限


def login_name(raw: str) -> str:
    """注册时存的登录名: 去首尾空白并转小写. 登录不用它, 见 login."""
    return raw.strip().lower()


@router.post("/login")
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> Token:
    """表单字段 username 为账号 (登录名). 只去首尾空白, 保留原写法: UserStore 查找不区分大小写,
    注册功能之前若有只差大小写的旧账号, 按原写法取完全相同的那个, 转小写就分不出来了."""
    found = await deps.users.by_email(form.username.strip())
    if found is None or not verify_password(form.password, found[1]):
        raise api_error("BAD_CREDENTIALS", "账号或密码不正确")
    return Token(access_token=create_access_token(found[0].id))


@router.post("/register", status_code=201)
async def register(body: RegisterIn, deps: Annotated[AgentDeps, Depends(get_deps)]) -> Token:
    """开放注册: 建账号后直接返回访问令牌. 账号已被占用 409 USERNAME_TAKEN, 各项不合要求 422 INVALID_VALUE."""
    username = login_name(body.username)
    full_name = body.full_name.strip()
    if not username or len(username) > USERNAME_MAX or any(char.isspace() for char in username):
        raise api_error("INVALID_VALUE", f"账号不能为空, 不能含空白字符, 最长 {USERNAME_MAX} 个字符")
    if not full_name or len(full_name) > FULL_NAME_MAX:
        raise api_error("INVALID_VALUE", f"姓名不能为空, 最长 {FULL_NAME_MAX} 个字符")
    low, high = PASSWORD_RANGE
    if not low <= len(body.password) <= high:
        raise api_error("INVALID_VALUE", f"密码长度须在 {low} 到 {high} 位之间")
    user = await deps.users.create(username, full_name, hash_password(body.password))
    if user is None:
        raise api_error("USERNAME_TAKEN", "账号已被占用")
    return Token(access_token=create_access_token(user.id))
