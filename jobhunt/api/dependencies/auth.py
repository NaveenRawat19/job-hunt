from fastapi import Depends

from services.auth_service import AuthService
from repositories.user_repository import UserRepository
from repositories.session_repository import SessionRepository
from repositories.profile_repository import ProfileRepository


def get_auth_service(
    db=Depends(get_db),
    settings=Depends(auth_settings)
):
    return AuthService(
        user_repository=UserRepository(db),
        session_repository=SessionRepository(db),
        profile_repository=ProfileRepository(db),
        settings=settings
    )