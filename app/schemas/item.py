from pydantic import BaseModel
from datetime import datetime


class ItemBase(BaseModel):
    title: str
    description: str | None = None
    price: float


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    price: float | None = None


class ItemResponse(ItemBase):
    id: int
    owner_id: int
    created_at: datetime

    class Config:
        from_attributes = True
