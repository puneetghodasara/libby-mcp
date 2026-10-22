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


class Card(BaseModel):
    id: str = Field(description="Libby card id.")
    library: str = Field(description="Library key, as used by the catalog tools.")
    name: str | None = None
    library_name: str | None = None
    loan_days: dict[str, int] = Field(
        default_factory=dict, description="Preferred loan length in days per format (ebook, audiobook, magazine)."
    )


class Loan(BaseModel):
    title_id: str
    title: str
    author: str | None = None
    media_type: str | None = None
    library: str
    checkout_date: str | None = None
    expires: str | None = None
    renewable_on: str | None = Field(None, description="Earliest renewal date.")
    libby_url: str


class Hold(BaseModel):
    title_id: str
    title: str
    author: str | None = None
    media_type: str | None = None
    library: str
    placed: str | None = None
    ready: bool = Field(description="Available to borrow now.")
    position: int | None = Field(None, description="Place in the hold queue.")
    estimated_wait_days: int | None = None
    auto_borrow: bool = False
    libby_url: str


class AccountSummary(BaseModel):
    cards: list[Card]
    loans: list[Loan]
    holds: list[Hold]


class HistoryItem(BaseModel):
    title_id: str
    title: str
    author: str | None = None
    media_type: str | None = None
    isbns: list[str] = Field(
        default_factory=list, description="ISBNs of this edition's formats, for matching elsewhere."
    )
    library: str
    subjects: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, description="The user's other Libby tags on it, e.g. 'Finished'.")
    status: Literal["on_loan", "on_hold", "returned"]
    due: str | None = Field(None, description="Loan due date, when on loan.")
    hold_position: int | None = Field(None, description="Place in the hold queue, when on hold.")
    hold_wait_days: int | None = Field(None, description="Estimated wait, when on hold.")
    borrow_count: int = Field(
        description="Known borrows. A minimum: repeat borrows are only logged on current cards since "
        "their reading history began."
    )
    borrow_dates: list[str] = Field(description="Known borrow times (ISO 8601, UTC), oldest first.")
    first_borrowed: str | None = None
    last_borrowed: str | None = None
    length_minutes: int | None = Field(None, description="Full length of the audiobook.")
    minutes_spent: int | None = Field(None, description="Time spent reading or listening, rounded down.")
    sessions: int | None = Field(None, description="How many times the title was opened.")
    marker_percent: float | None = Field(
        None, description="Where the reading position is, as % of the book. Can be ahead of what was heard."
    )
    marker_saved: str | None = Field(None, description="When the reading position was last saved.")
    marker_chapter: int | None = Field(None, description="Ebooks: index of the chapter the reading position is in.")
    marker_chapter_title: str | None = Field(None, description="Ebooks: that chapter's title, where Libby records one.")
    last_opened: str | None = None
    listened_percent: float | None = Field(
        None, description="minutes_spent as % of length_minutes: how much of the audiobook was actually heard."
    )
    marker_gap_percent: float | None = Field(
        None,
        description="marker_percent minus listened_percent: the share of the audiobook the marker passed without "
        "being heard. Up to about 5: at most an intro or preface skipped; above about 10: skipped ahead; "
        "negative: parts were listened to more than once. More reliable than pace early in a book.",
    )
    pace: float | None = Field(
        None,
        description="Length covered by the marker divided by time spent. About 1: listened straight through "
        "at 1x; about 1.3-2: faster playback or some skipping; above 2: skipped ahead.",
    )
    libby_url: str


class HistoryResult(BaseModel):
    items: list[HistoryItem] = Field(description="Most recently borrowed first.")
    total: int = Field(description="All titles in the borrowing history, before `library` and `limit`.")
