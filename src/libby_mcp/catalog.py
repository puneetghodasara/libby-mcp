"""Async client for OverDrive's public catalog API, the one the Libby app uses.

No login is needed. The API is undocumented, so every field is read defensively.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any
from urllib.parse import quote

import httpx

from .models import Availability, Category, Library, MediaType, Sort, Subject, Title, TitleDetails

THUNDER_URL = "https://thunder.api.overdrive.com/v2"
LOCATE_URL = "https://locate.libbyapp.com/autocomplete"
SHARE_URL = "https://share.libbyapp.com/title/{id}"

_SORT = {"relevance": "relevance", "popular": "mostpopular", "newest": "newlyadded"}
_RETRY_STATUS = {429, 500, 502, 503, 504}
_TAG = re.compile(r"<[^>]+>")
_NON_WORD = re.compile(r"[^a-z0-9]+")


class CatalogError(Exception):
    """The catalog API rejected a request or could not be reached."""


class Catalog:
    def __init__(self, http: httpx.AsyncClient, *, retries: int = 2, backoff: float = 0.5) -> None:
        self._http = http
        self._retries = retries
        self._backoff = backoff

    async def find_libraries(self, query: str, limit: int = 10) -> list[Library]:
        data = await self._get(f"{LOCATE_URL}/{quote(query, safe='')}")
        cities: dict[int, set[str]] = {}
        for branch in data.get("branches") or []:
            for system in branch.get("systems") or []:
                if (website_id := system.get("websiteId")) is not None:
                    cities.setdefault(website_id, set()).add(branch.get("city") or "")
        if not cities:
            return []
        ids = list(cities)[:limit]
        libs = await self._get(f"{THUNDER_URL}/libraries", {"websiteIds": ",".join(map(str, ids)), "perPage": limit})
        return [
            Library(
                key=lib["preferredKey"],
                name=lib["name"],
                website_id=lib["websiteId"],
                locations=sorted(c for c in cities.get(lib["websiteId"], ()) if c),
            )
            for lib in libs.get("items") or []
        ]

    async def library_keys(self, website_ids: list[int | str]) -> dict[str, str]:
        """Library keys for OverDrive website ids."""
        params = {"websiteIds": ",".join(map(str, website_ids)), "perPage": len(website_ids)}
        data = await self._get(f"{THUNDER_URL}/libraries", params)
        return {str(lib["websiteId"]): lib["preferredKey"] for lib in data.get("items") or []}

    async def search(
        self,
        library: str,
        *,
        query: str | None = None,
        media_type: MediaType | None = None,
        available_only: bool = False,
        language: str | None = None,
        subject_ids: list[str] | None = None,
        category_codes: list[str] | None = None,
        sort: Sort = "relevance",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"perPage": limit, "sortBy": _SORT[sort]}
        if query:
            params["query"] = query
        if media_type:
            params["mediaTypes"] = media_type
        if available_only:
            params["showOnlyAvailable"] = "true"
        if language:
            params["language"] = language
        if subject_ids:
            params["subject"] = subject_ids
        if category_codes:
            params["bisacCode"] = category_codes
        data = await self._get(f"{THUNDER_URL}/libraries/{library}/media", params)
        return data.get("items") or []

    async def availability(self, library: str, title_ids: list[str]) -> dict[str, Availability]:
        """Availability at one library, keyed by title id. Titles it doesn't own are left out."""
        data = await self._get(
            f"{THUNDER_URL}/libraries/{library}/media/availability", {"titleIds": ",".join(title_ids)}
        )
        items = (item for item in data.get("items") or [] if item and item.get("isOwned", True))
        return {str(item["id"]): to_availability(library, item) for item in items}

    async def titles(self, title_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Raw catalog items by id, looked up in batches."""
        batches = [title_ids[i : i + 25] for i in range(0, len(title_ids), 25)]
        results = await asyncio.gather(
            *(self._get(f"{THUNDER_URL}/media/bulk", {"titleIds": ",".join(batch)}) for batch in batches)
        )
        return {str(item["id"]): item for items in results for item in items or [] if item}

    async def title(self, title_id: str) -> TitleDetails:
        return to_details(await self._get(f"{THUNDER_URL}/media/{title_id}"))

    async def subjects(self, library: str, media_type: MediaType | None = None) -> list[Subject]:
        params: dict[str, Any] = {"perPage": 1, "includeFacets": "true"}
        if media_type:
            params["mediaTypes"] = media_type
        data = await self._get(f"{THUNDER_URL}/libraries/{library}/media", params)
        items = ((data.get("facets") or {}).get("subjects") or {}).get("items") or []
        return [Subject(id=str(s["id"]), name=s["name"], count=s.get("totalItems", 0)) for s in items]

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            try:
                response = await self._http.get(url, params=params)
            except httpx.TransportError as exc:
                if last:
                    raise CatalogError(f"catalog unreachable: {exc}") from exc
            else:
                if response.status_code not in _RETRY_STATUS or last:
                    break
            await asyncio.sleep(self._backoff * 2**attempt)
        if response.is_error:
            raise CatalogError(_error_message(response))
        try:
            return response.json()
        except ValueError as exc:
            raise CatalogError(f"catalog API returned invalid JSON from {response.url}") from exc


def to_title(item: dict[str, Any], availability: list[Availability] | None = None) -> Title:
    creators = item.get("creators") or []
    series = item.get("detailedSeries") or {}
    formats = item.get("formats") or []
    return Title(
        id=str(item["id"]),
        title=item.get("title") or "",
        subtitle=item.get("subtitle") or None,
        authors=[c["name"] for c in creators if c.get("role") == "Author"],
        narrators=[c["name"] for c in creators if c.get("role") == "Narrator"],
        media_type=(item.get("type") or {}).get("id") or "unknown",
        language=next((lang["id"] for lang in item.get("languages") or []), None),
        series=series.get("seriesName") or item.get("series") or None,
        series_position=series.get("readingOrder") or None,
        subjects=[s["name"] for s in item.get("subjects") or []],
        categories=[Category(code=b["code"], name=b.get("description") or b["code"]) for b in item.get("bisac") or []],
        published=(item.get("publishDate") or "")[:10] or None,
        duration=next((f["duration"] for f in formats if f.get("duration")), None),
        star_rating=item.get("starRating") or None,
        availability=availability or [],
        libby_url=SHARE_URL.format(id=item["id"]),
    )


def work_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """Identify a work across editions: libraries in different countries often own
    different editions (different title ids) of the same book."""
    author = next((c["name"] for c in item.get("creators") or [] if c.get("role") == "Author"), "")
    media_type = (item.get("type") or {}).get("id") or ""
    return _normalise(item.get("title")), _normalise(author), media_type


def to_details(item: dict[str, Any]) -> TitleDetails:
    isbns = {f["isbn"] for f in item.get("formats") or [] if f.get("isbn")}
    return TitleDetails(
        **to_title(item).model_dump(),
        description=_plain_text(item.get("description")),
        publisher=(item.get("publisher") or {}).get("name"),
        isbns=sorted(isbns),
    )


def to_availability(library: str, item: dict[str, Any]) -> Availability:
    available = bool(item.get("isAvailable"))
    return Availability(
        library=library,
        title_id=str(item["id"]),
        available=available,
        always_available=item.get("availabilityType") == "always",
        available_copies=item.get("availableCopies"),
        owned_copies=item.get("ownedCopies"),
        holds=item.get("holdsCount") or 0,
        estimated_wait_days=None if available else item.get("estimatedWaitDays"),
        lucky_day_copies=item.get("luckyDayAvailableCopies") or 0,
    )


def _normalise(value: str | None) -> str:
    return _NON_WORD.sub(" ", (value or "").lower()).strip()


def _plain_text(value: str | None) -> str | None:
    if not value:
        return None
    text = _TAG.sub(" ", value.replace("<br>", "\n").replace("</p>", "\n"))
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = body.get("message") or body.get("errorCode")
    except ValueError:
        detail = None
    return f"catalog API returned {response.status_code}" + (f": {detail}" if detail else "")
