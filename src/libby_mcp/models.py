"""Tool result models, normalised from OverDrive's catalog API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MediaType = Literal["ebook", "audiobook", "magazine", "video"]
Sort = Literal["relevance", "popular", "newest"]


class Library(BaseModel):
    key: str = Field(description="Library key used by every other tool, e.g. 'spl'.")
    name: str
    website_id: int
    locations: list[str] = Field(default_factory=list, description="Cities with a branch.")


class Availability(BaseModel):
    library: str = Field(description="Library key.")
    title_id: str = Field(description="The edition this library owns; it can differ between libraries.")
    available: bool
    always_available: bool = Field(False, description="Unlimited simultaneous loans; copies are null.")
    available_copies: int | None = None
    owned_copies: int | None = None
    holds: int
    estimated_wait_days: int | None = Field(None, description="Libby's estimate; null when available.")
    lucky_day_copies: int = Field(0, description="Skip-the-line copies available now.")


class Title(BaseModel):
    id: str = Field(description="OverDrive title id of one edition. Each availability entry names its own edition.")
    title: str
    subtitle: str | None = None
    authors: list[str]
    narrators: list[str] = Field(default_factory=list)
    media_type: str
    language: str | None = None
    series: str | None = None
    series_position: str | None = None
    subjects: list[str] = Field(default_factory=list)
    published: str | None = Field(None, description="Publish date, YYYY-MM-DD.")
    duration: str | None = Field(None, description="Audiobook length, HH:MM:SS.")
    star_rating: float | None = None
    availability: list[Availability] = Field(default_factory=list)
    libby_url: str


class TitleDetails(Title):
    description: str | None = None
    publisher: str | None = None
    isbns: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    titles: list[Title]
    errors: dict[str, str] = Field(default_factory=dict, description="Libraries that failed, with the reason.")


class AvailabilityResult(BaseModel):
    availability: dict[str, list[Availability]] = Field(description="Per title id.")
    errors: dict[str, str] = Field(default_factory=dict)


class Subject(BaseModel):
    id: str
    name: str
    count: int
