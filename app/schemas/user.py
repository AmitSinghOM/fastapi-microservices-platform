from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    StringConstraints,
    field_validator,
    model_validator,
)

Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]


class UserBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    name: Name


class UserCreate(UserBase):
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        size = len(value.encode("utf-8"))
        if size < 8:
            raise ValueError("Password must be at least 8 bytes")
        if size > 72:
            raise ValueError("Password must be at most 72 bytes")
        return value


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = None
    name: Name | None = None

    @field_validator("email", "name")
    @classmethod
    def reject_null(cls, value, info):
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
        return value

    @model_validator(mode="after")
    def require_change(self) -> "UserUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class UserResponse(UserBase):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    created_at: datetime
    is_active: bool
