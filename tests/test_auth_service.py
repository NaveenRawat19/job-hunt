"""Focused tests for the auth use case; no PostgreSQL server required."""
from __future__ import annotations

import asyncio

from jobhunt.api.auth import load_settings
from jobhunt.services.auth_service import AuthService


class FakeDatabase:
    def __init__(self):
        self.users = {}
        self.next_id = 1

    async def create_user(self, email, password_hash):
        if email in self.users:
            return None
        user = {
            "id": self.next_id, "email": email, "created_at": "now",
            "password_hash": password_hash, "failed_attempts": 0,
            "locked_until": None,
        }
        self.next_id += 1
        self.users[email] = user
        return user

    async def get_user_by_email(self, email):
        return self.users.get(email)

    async def lock_remaining(self, user):
        return 0

    async def record_failed_login(self, *_args):
        return None

    async def reset_failed_logins(self, *_args):
        return None

    async def create_session(self, _user_id):
        return "test-session"

    async def get_profile(self, _user_id):
        return None


def test_register_and_login(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "s" * 40)
    service = AuthService(FakeDatabase(), load_settings())

    async def exercise():
        user = await service.register("USER@example.com", "a secure password")
        assert user["email"] == "user@example.com"
        assert user["password_hash"].startswith("scrypt$")

        result = await service.login("USER@example.com", "a secure password")
        assert result["token_type"] == "bearer"
        assert result["has_profile"] is False
        assert result["access_token"]

    asyncio.run(exercise())
