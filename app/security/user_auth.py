"""
Lightweight user authentication helpers for issuing and validating bearer tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Tuple

from fastapi import HTTPException, status

from app.config import settings


DEFAULT_ITERATIONS = 260_000


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    """
    Create a pbkdf2_sha256 hash string for storage.
    Format: pbkdf2_sha256$<iterations>$<salt>$<hash_hex>
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify either a plain password or a pbkdf2_sha256 hash string."""
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iter_str, salt, digest_hex = stored.split("$", 3)
            iterations = int(iter_str)
            derived = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("utf-8"),
                iterations,
            ).hex()
            return hmac.compare_digest(derived, digest_hex)
        except Exception:
            return False
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))


class UserAuthManager:
    """Issue and validate stateless bearer tokens for users."""

    def __init__(self, secret_key: str, default_ttl_minutes: int = 7 * 24 * 60) -> None:
        self._secret = secret_key.encode("utf-8")
        self._default_ttl_minutes = default_ttl_minutes

    def issue_token(self, user_id: str, username: str) -> Tuple[str, float]:
        expires_at = int(time.time() + self._default_ttl_minutes * 60)
        payload = f"{user_id}:{username}:{expires_at}:{secrets.token_hex(8)}"
        signature = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).digest()
        token = (
            base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8").rstrip("=")
            + "."
            + base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
        )
        return token, float(expires_at)

    @staticmethod
    def _add_padding(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return (value + padding).encode("utf-8")

    def _parse_token(self, token: str) -> Tuple[str, str, int]:
        try:
            payload_b64, signature_b64 = token.split(".", 1)
            payload_bytes = base64.urlsafe_b64decode(self._add_padding(payload_b64))
            signature_bytes = base64.urlsafe_b64decode(self._add_padding(signature_b64))
            expected_sig = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
            if not hmac.compare_digest(signature_bytes, expected_sig):
                raise ValueError("Invalid signature")
            payload = payload_bytes.decode("utf-8")
            user_id, username, exp_str, _nonce = payload.split(":", 3)
            return user_id, username, int(exp_str)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            ) from exc

    def validate_token(self, token: str) -> Tuple[str, str]:
        """Validate token and return (user_id, username)."""
        user_id, username, exp_ts = self._parse_token(token)
        if exp_ts < int(time.time()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Login session expired. Please sign in again.",
            )
        return user_id, username


user_auth_manager = UserAuthManager(
    secret_key=settings.secret_key,
    default_ttl_minutes=getattr(settings, "user_token_ttl_minutes", 7 * 24 * 60),
)
