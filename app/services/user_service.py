"""
User management service.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.i18n import normalize as normalize_locale
from app.i18n.messages import t
from app.models.user import User
from app.security.admin_auth import AdminUser
from app.security.user_auth import hash_password, verify_password
from app.services.base import BaseService


VALID_ROLES = ("user", "admin")
DEFAULT_PROFILE_ROLE = "developer"
MAX_DISABLED_MESSAGE_LENGTH = 1000
PROFILE_ROLE_ALIASES = {
    "dev": "developer",
    "develop": "developer",
    "qa": "tester",
    "test": "tester",
    "testing": "tester",
}
PROFILE_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _normalize_role(role: Optional[str]) -> str:
    """Coerce role string to a known value, defaulting to ``user``."""
    if not role:
        return "user"
    value = str(role).strip().lower()
    if value in VALID_ROLES:
        return value
    if value in ("superuser", "ops", "administrator", "root"):
        return "admin"
    return "user"


def _normalize_optional_text(value: Optional[str]) -> Optional[str]:
    """Trim optional profile text and store blanks as null."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_disabled_message(message: Optional[str]) -> Optional[str]:
    """Trim the admin's disable note, storing blanks as null."""
    if message is None:
        return None
    normalized = str(message).strip()
    if not normalized:
        return None
    return normalized[:MAX_DISABLED_MESSAGE_LENGTH]


def _normalize_profile_role(role: Optional[str]) -> str:
    """Normalize an extensible profile role without tying it to permissions."""
    if not role:
        return DEFAULT_PROFILE_ROLE
    value = str(role).strip().lower().replace("-", "_").replace(" ", "_")
    value = PROFILE_ROLE_ALIASES.get(value, value)
    if PROFILE_ROLE_PATTERN.match(value):
        return value
    return DEFAULT_PROFILE_ROLE


CLARIFICATION_ON_TIMEOUT_VALUES = ("cancel", "continue")
CLARIFICATION_MAX_ROUNDS_MIN = 0
CLARIFICATION_MAX_ROUNDS_MAX = 20


def _normalize_clarification_on_timeout(value: Optional[str]) -> str:
    """Coerce the clarification timeout behaviour to a known value."""
    if not value:
        return "cancel"
    normalized = str(value).strip().lower()
    return normalized if normalized in CLARIFICATION_ON_TIMEOUT_VALUES else "cancel"


def _clamp_clarification_max_rounds(value: Optional[int]) -> int:
    """Clamp the per-run clarification cap into a sane range (default 5)."""
    if value is None:
        return 5
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return 5
    return max(CLARIFICATION_MAX_ROUNDS_MIN, min(CLARIFICATION_MAX_ROUNDS_MAX, rounds))


class UserService(BaseService):
    """Encapsulate user CRUD and authentication."""

    async def get_by_id(self, db: AsyncSession, user_id: str) -> Optional[User]:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_username(self, db: AsyncSession, username: str) -> Optional[User]:
        result = await db.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()

    async def create_user(
        self,
        db: AsyncSession,
        *,
        username: str,
        password: str,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        role: str = "user",
        profile_role: Optional[str] = None,
        initialize_last_login: bool = False,
    ) -> User:
        existing = await self.get_by_username(db, username)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已存在",
            )
        now = datetime.utcnow() if initialize_last_login else None
        user = User(
            username=username,
            password_hash=hash_password(password),
            display_name=_normalize_optional_text(display_name) or username,
            email=_normalize_optional_text(email),
            role=_normalize_role(role),
            profile_role=_normalize_profile_role(profile_role),
            last_login_at=now,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="保存用户失败",
            ) from exc
        await db.refresh(user)
        return user

    async def authenticate(
        self,
        db: AsyncSession,
        *,
        username: str,
        password: str,
        locale: str = "zh",
    ) -> User:
        user = await self.get_by_username(db, username)
        if not user or not verify_password(password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
            )
        if not user.is_active:
            # Only surface the disabled state (and the admin's note) once the
            # password checks out, so this never becomes an account-probing
            # oracle for someone guessing usernames.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(user.disabled_message or "").strip()
                or t("auth.account_disabled", locale),
            )
        user.last_login_at = datetime.utcnow()
        await db.flush()
        await db.refresh(user)
        return user

    async def list_users(self, db: AsyncSession, *, offset: int = 0, limit: int = 50) -> List[User]:
        result = await db.execute(
            select(User)
            .order_by(User.created_at.desc())
            .offset(offset)
            .limit(limit),
        )
        return result.scalars().all()

    async def ensure_admin_users(self, db: AsyncSession, admin_users: List[AdminUser]) -> None:
        """Sync admin auth users into users table so they can be managed in UI."""
        if not admin_users:
            return
        dirty = False
        for admin_user in admin_users:
            if not admin_user.username:
                continue
            user = await self.get_by_username(db, admin_user.username)
            expected_is_active = not admin_user.disabled
            if user:
                # Ensure existing users from admin_auth.yaml are tagged as admin role
                if user.role != "admin":
                    user.role = "admin"
                    dirty = True
                continue
            password_hash = (
                admin_user.password_hash
                or (hash_password(admin_user.password) if admin_user.password else None)
                or hash_password(secrets.token_urlsafe(24))
            )
            user = User(
                username=admin_user.username,
                display_name=admin_user.username,
                email=None,
                password_hash=password_hash,
                is_active=expected_is_active,
                role="admin",
                profile_role=DEFAULT_PROFILE_ROLE,
            )
            db.add(user)
            dirty = True
        if dirty:
            await db.flush()

    async def update_user(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        is_active: Optional[bool] = None,
        role: Optional[str] = None,
        language: Optional[str] = None,
        disabled_message: Optional[str] = None,
        update_disabled_message: bool = False,
    ) -> Optional[User]:
        user = await self.get_by_id(db, user_id)
        if not user:
            return None
        if display_name is not None:
            user.display_name = display_name
        if email is not None:
            user.email = email
        if is_active is not None:
            if is_active:
                # Re-enabling retires the note: it only describes why the
                # account was locked, and a stale note would resurface on a
                # later disable.
                user.disabled_message = None
                user.disabled_at = None
            elif user.is_active:
                user.disabled_at = datetime.utcnow()
            user.is_active = is_active
        if update_disabled_message and not user.is_active:
            user.disabled_message = _normalize_disabled_message(disabled_message)
        if role is not None:
            user.role = _normalize_role(role)
        if language is not None:
            # Coerce unsupported codes to a supported one; never store as-is.
            user.language = normalize_locale(language)
        await db.flush()
        await db.refresh(user)
        return user

    async def update_profile(
        self,
        db: AsyncSession,
        user_id: str,
        *,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        language: Optional[str] = None,
        profile_role: Optional[str] = None,
        clarification_enabled: Optional[bool] = None,
        clarification_max_rounds: Optional[int] = None,
        clarification_on_timeout: Optional[str] = None,
        update_display_name: bool = False,
        update_email: bool = False,
        update_language: bool = False,
        update_profile_role: bool = False,
        update_clarification_enabled: bool = False,
        update_clarification_max_rounds: bool = False,
        update_clarification_on_timeout: bool = False,
    ) -> Optional[User]:
        """Update the self-service profile, including explicit null clears."""
        user = await self.get_by_id(db, user_id)
        if not user:
            return None
        if update_display_name:
            user.display_name = _normalize_optional_text(display_name)
        if update_email:
            user.email = _normalize_optional_text(email)
        if update_language:
            user.language = normalize_locale(language)
        if update_profile_role:
            user.profile_role = _normalize_profile_role(profile_role)
        if update_clarification_enabled:
            user.clarification_enabled = bool(clarification_enabled)
        if update_clarification_max_rounds:
            user.clarification_max_rounds = _clamp_clarification_max_rounds(
                clarification_max_rounds
            )
        if update_clarification_on_timeout:
            user.clarification_on_timeout = _normalize_clarification_on_timeout(
                clarification_on_timeout
            )
        await db.flush()
        await db.refresh(user)
        return user

    async def change_password(
        self,
        db: AsyncSession,
        user: User,
        *,
        current_password: str,
        new_password: str,
        locale: str,
    ) -> None:
        current_hash = user.password_hash
        if not await run_in_threadpool(verify_password, current_password, current_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=t("auth.current_password_incorrect", locale),
            )
        if current_password == new_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=t("auth.new_password_unchanged", locale),
            )

        new_hash = await run_in_threadpool(hash_password, new_password)
        # An admin reset or another change may have superseded this password.
        result = await db.execute(
            update(User)
            .where(User.id == user.id, User.is_active.is_(True), User.password_hash == current_hash)
            .values(password_hash=new_hash)
            .returning(User.id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=t("auth.password_change_conflict", locale),
            )

    async def set_password(self, db: AsyncSession, user_id: str, new_password: str) -> Optional[User]:
        user = await self.get_by_id(db, user_id)
        if not user:
            return None
        user.password_hash = hash_password(new_password)
        await db.flush()
        await db.refresh(user)
        return user


user_service = UserService()
