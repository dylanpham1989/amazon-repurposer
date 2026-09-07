"""DTO dùng chung giữa các service."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DescSource = Literal["product_description", "bullets", "aplus", "none"]
FetchVia = Literal["cache", "direct", "scrapedo"]


class ProductData(BaseModel):
    asin: str
    marketplace: str
    title: str = ""
    description: str = ""
    desc_source: DescSource = "none"
    bullets: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    brand: str | None = None
    price: str | None = None
    is_best_seller: bool = False
    has_free_delivery: bool = False
    warnings: list[str] = Field(default_factory=list)


class ParsedUrl(BaseModel):
    asin: str
    marketplace: str
    domain: str
    original: str
