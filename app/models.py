from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(320), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    items = relationship(
        "Item",
        back_populates="owner",
        cascade="all, delete-orphan",
    )


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_items_price_nonnegative"),
        Index("ix_items_owner_created", "owner_id", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True)
    title = Column(String(200), index=True, nullable=False)
    description = Column(String(2_000), nullable=True)
    price = Column(Numeric(12, 2), nullable=False)
    owner_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), nullable=False)

    owner = relationship("User", back_populates="items")
