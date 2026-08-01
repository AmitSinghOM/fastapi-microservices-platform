from pydantic import BaseModel


class TokenResponse(BaseModel):
    """OAuth2 bearer token response."""

    access_token: str
    token_type: str = "bearer"
