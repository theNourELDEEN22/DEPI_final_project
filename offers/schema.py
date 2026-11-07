"""D1 - Standard offer schema with versioning (Epic D: Offers).

Three offer types fit one schema: discount, buy2get1, bundle.
Fields that do not belong to the offer's type must be None.
Any change creates a NEW version (new row, version + 1); offer_id stays the same.
Requires: pydantic>=2
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OfferType(str, Enum):
    DISCOUNT = "discount"
    BUY2GET1 = "buy2get1"
    BUNDLE = "bundle"


class OfferStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    ACTIVE = "active"
    STOPPED = "stopped"
    ENDED = "ended"


class BundleItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    product_id: str
    quantity: int = Field(gt=0)


class Offer(BaseModel):
    model_config = ConfigDict(frozen=True)  # immutable: changes = new version

    # --- identity and versioning ---
    offer_id: str = Field(default_factory=lambda: str(uuid4()))
    version: int = Field(default=1, ge=1)
    branch_id: str
    type: OfferType
    status: OfferStatus = OfferStatus.DRAFT

    # --- common fields ---
    product_ids: list[str] = Field(min_length=1)
    start_date: datetime
    end_date: datetime
    quantity_cap: Optional[int] = Field(default=None, gt=0)
    evidence_ref: Optional[str] = None   # link to the stock signal (H1)
    forecast_ref: Optional[str] = None   # link to the demand forecast (E5)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    approval_hash: Optional[str] = None  # set by the approval service (A12)

    # --- type-specific fields (None when unused) ---
    discount_percent: Optional[Decimal] = Field(default=None, gt=0, le=100)
    buy_qty: Optional[int] = Field(default=None, gt=0)
    get_qty: Optional[int] = Field(default=None, gt=0)
    bundle_items: Optional[list[BundleItem]] = None
    bundle_price: Optional[Decimal] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_type_fields(self) -> "Offer":
        used = {
            OfferType.DISCOUNT: {"discount_percent"},
            OfferType.BUY2GET1: {"buy_qty", "get_qty"},
            OfferType.BUNDLE: {"bundle_items", "bundle_price"},
        }
        all_specific = {"discount_percent", "buy_qty", "get_qty", "bundle_items", "bundle_price"}
        mine = used[self.type]

        for f in mine:
            if getattr(self, f) is None:
                raise ValueError(f"{self.type.value} offer requires '{f}'")
        for f in all_specific - mine:
            if getattr(self, f) is not None:
                raise ValueError(f"'{f}' must be null for a {self.type.value} offer")

        if self.type == OfferType.BUY2GET1 and (self.buy_qty, self.get_qty) != (2, 1):
            raise ValueError("buy2get1 requires buy_qty=2 and get_qty=1")
        if self.type == OfferType.BUNDLE and len(self.bundle_items) < 2:
            raise ValueError("bundle needs at least 2 items")
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self

    def new_version(self, **changes) -> "Offer":
        """Return a new Offer: same offer_id, version + 1, back to draft, approval cleared."""
        data = self.model_dump()
        data.update(changes)
        data.update(
            version=self.version + 1,
            status=OfferStatus.DRAFT,
            approval_hash=None,
            created_at=datetime.now(timezone.utc),
        )
        return Offer(**data)

    def content_hash(self) -> str:
        """Hash of the commercial content. Used by the approval service so an
        approval of version N never matches version N+1 (D8)."""
        payload = self.model_dump(
            mode="json", exclude={"status", "approval_hash", "created_at"}
        )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()