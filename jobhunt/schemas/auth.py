from pydantic import BaseModel
from datetime import datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    idle_timeout_seconds: int
    has_profile: bool
    expires_at: datetime