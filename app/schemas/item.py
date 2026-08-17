from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
Description = Annotated[str, StringConstraints(max_length=2_000)]
Price = Annotated[float, Field(allow_inf_nan=False)]


def validate_money(value: float) -> float:
    decimal_value = Decimal(str(value))
    if abs(decimal_value) >= Decimal("10000000000"):
        raise ValueError("price must be less than 10,000,000,000 in magnitude")
    if decimal_value != decimal_value.quantize(Decimal("0.01")):
        raise ValueError("price must have at most two decimal places")
    return value


class ItemBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    description: Description | None = None
    price: Price

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: float) -> float:
        return validate_money(value)


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    description: Description | None = None
    price: Price | None = None

    @field_validator("title", "price")
    @classmethod
    def reject_null(cls, value, info):
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
        return value

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: float | None) -> float | None:
        return validate_money(value) if value is not None else value

    @model_validator(mode="after")
    def require_change(self) -> "ItemUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class ItemResponse(ItemBase):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    owner_id: int
    created_at: datetime
