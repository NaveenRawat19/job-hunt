"""Authentication use cases; persistence is asynchronous, crypto runs off-loop."""
from __future__ import annotations
from datetime import datetime, timezone
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from ..api.auth import AuthSettings, burn_password_check, create_token, hash_password, verify_password

MAX_PASSWORD_LEN = 128
BEARER = {"WWW-Authenticate": "Bearer"}

class AuthService:
    def __init__(self, database, settings: AuthSettings):
        self.database, self.settings = database, settings

    async def register(self, username: str, email: str, password: str) -> dict | None:
        password_hash = await run_in_threadpool(hash_password, password)
        return await self.database.create_user(username, email.strip().lower(), password_hash)

    async def login(self, email: str, password: str) -> dict:
        email = email.strip().lower()
        user = await self.database.get_user_by_email(email)
        if user:
            remaining = await self.database.lock_remaining(user)
            if remaining:
                raise HTTPException(429, "too many failed login attempts; try again later",
                    headers={**BEARER, "Retry-After": str(remaining)})
        if len(password) > MAX_PASSWORD_LEN:
            raise HTTPException(401, "incorrect email or password", headers=BEARER)
        if not user:
            await run_in_threadpool(burn_password_check, password)
            raise HTTPException(401, "incorrect email or password", headers=BEARER)
        valid = await run_in_threadpool(verify_password, password, user["password_hash"])
        if not valid:
            await self.database.record_failed_login(user["id"], self.settings.max_failed_logins,
                                                    self.settings.lockout_seconds)
            raise HTTPException(401, "incorrect email or password", headers=BEARER)
        if user["failed_attempts"]:
            await self.database.reset_failed_logins(user["id"])
        session_id = await self.database.create_session(user["id"])
        token, expires_at = create_token(self.settings, user["id"], session_id)
        return {
            "access_token": token,
            "token_type": "bearer",
            "idle_timeout_seconds": self.settings.idle_seconds,
            "expires_at": datetime.fromtimestamp(
                expires_at, timezone.utc
            ).isoformat(),
            "has_profile": user["has_profile"]
        }
