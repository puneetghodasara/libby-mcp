"""Client for the Libby account API: link a device, then read and change loans and holds.

Linking uses Libby's "Copy To Another Device" setup code. The resulting token expires
after 7 days unless refreshed, so it is refreshed on use and saved to a local file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .catalog import SHARE_URL, Catalog, CatalogError
from .models import AccountSummary, Card, HistoryItem, HistoryResult, Hold, Loan

SENTRY_URL = "https://sentry.libbyapp.com"
VANDAL_URL = "https://vandal.libbyapp.com"  # Libby's tags, including its automatic borrowing history
_CODE = re.compile(r"^\d{8}$")
DEFAULT_LOAN_DAYS = 14  # when a card has no preference
PROGRESS_CONCURRENCY = 4  # parallel reading-progress lookups

CONNECT_HINT = (
    "Ask the user for a Libby setup code: in the Libby app, open the menu and choose "
    "'Copy To Another Device' to show an 8-digit code, then call connect_account with it."
)


class AccountError(Exception):
    """The account API rejected a request or could not be reached."""


class NotConnected(AccountError):
    def __init__(self) -> None:
        super().__init__(f"No Libby account is linked. {CONNECT_HINT}")


def default_identity_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "libby-mcp" / "identity.json"


class Account:
    def __init__(self, http: httpx.AsyncClient, catalog: Catalog, path: Path | None = None) -> None:
        self._http = http
        self._catalog = catalog
        self._path = path
        self._token: str | None = None
        self._chip: str | None = None  # identity id; a refresh that changes it means the link was lost
        self._refreshed = False
        self._cards: list[Card] | None = None

    async def connect(self, code: str) -> list[Card]:
        code = code.strip().replace(" ", "").replace("-", "")
        if not _CODE.match(code):
            raise AccountError("A Libby setup code is 8 digits.")
        await self._refresh_token(new=True)
        try:
            await self._request("POST", "/chip/clone/code", data={"code": code})
        except AccountError as exc:
            raise AccountError(
                f"Libby didn't accept the setup code ({exc}); codes expire quickly. {CONNECT_HINT}"
            ) from exc
        await self._refresh_token(keep_identity=False)  # linking may switch identity
        cards = await self.cards(refresh=True)
        if not cards:
            raise NotConnected
        return cards

    async def cards(self, *, refresh: bool = False) -> list[Card]:
        """Linked library cards; an empty list when no account is linked."""
        if self._cards is None or refresh:
            try:
                await self._sync()
            except NotConnected:
                return []
        return self._cards or []

    async def summary(self) -> AccountSummary:
        data = await self._sync()
        library = {card.id: card.library for card in self._cards or []}
        return AccountSummary(
            cards=self._cards or [],
            loans=[_loan(item, library) for item in data.get("loans") or []],
            holds=[_hold(item, library) for item in data.get("holds") or []],
        )

    async def history(self, library: str | None, limit: int, *, progress: bool = True) -> HistoryResult:
        """Titles borrowed in Libby, newest first, from Libby's automatic borrowing tag.

        With `progress`, each title also gets how far the user read or listened.
        """
        cards = await self.cards()
        if not cards:
            raise NotConnected
        tags = await self._tags()
        borrowed = next((t for t in tags if "borrowed" in (t.get("behaviors") or {})), None)
        if borrowed is None:
            return HistoryResult(items=[], total=0)
        other: dict[str, list[str]] = {}
        for tag in tags:
            if tag is not borrowed:
                for tagging in tag.get("taggings") or []:
                    other.setdefault(str(tagging["titleId"]), []).append(tag["name"])
        taggings = sorted(borrowed.get("taggings") or [], key=lambda t: t.get("createTime") or 0, reverse=True)
        keys = await self._library_keys({str(t.get("websiteId")) for t in taggings})
        if library:
            taggings = [t for t in taggings if keys.get(str(t.get("websiteId"))) == library.strip().lower()]
        taggings = taggings[:limit]
        try:
            details = await self._catalog.titles([str(t["titleId"]) for t in taggings])
        except CatalogError:
            details = {}
        items = [_history(t, details.get(str(t["titleId"])), keys, other) for t in taggings]
        if progress:
            # Progress is kept per library: read it through the user's current card there.
            card_for = {card.library: card for card in cards}
            limiter = asyncio.Semaphore(PROGRESS_CONCURRENCY)
            await asyncio.gather(*(self._add_progress(item, card_for.get(item.library), limiter) for item in items))
        return HistoryResult(items=items, total=len(borrowed.get("taggings") or []))

    async def _add_progress(self, item: HistoryItem, card: Card | None, limiter: asyncio.Semaphore) -> None:
        kind = {"audiobook": "audiobook", "ebook": "book"}.get(item.media_type or "")
        if card is None or kind is None:
            return
        async with limiter:
            try:
                data = await self._request("GET", f"/card/{card.id}/{kind}/data/{item.title_id}")
            except AccountError:
                return  # e.g. 404 missing_stamp: no reading record for this title
        stats = data.get("statistics") or {}
        opened = (data.get("timestamps") or {}).get("accessed")
        item.progress = round(((data.get("position") or {}).get("percentageOfBook") or 0.0), 3)
        item.time_spent_minutes = round((stats.get("readingTime") or 0) / 60)
        item.sessions = stats.get("accesses")
        item.last_opened = datetime.fromtimestamp(opened, UTC).isoformat() if opened else None

    async def _tags(self) -> list[dict[str, Any]]:
        """All of the user's Libby tags with every tagging (the list endpoint previews only a few)."""
        tags = (await self._request("GET", f"{VANDAL_URL}/tags")).get("tags") or []

        async def complete(tag: dict[str, Any]) -> None:
            if (tag.get("totalTaggings") or 0) > len(tag.get("taggings") or []):
                path = f"{VANDAL_URL}/tag/{tag['uuid']}/{quote(tag['name'], safe='')}"
                tag["taggings"] = ((await self._request("GET", path)).get("tag") or {}).get("taggings") or []

        await asyncio.gather(*(complete(tag) for tag in tags))
        return tags

    async def _library_keys(self, website_ids: set[str]) -> dict[str, str]:
        try:
            return await self._catalog.library_keys(sorted(website_ids)) if website_ids else {}
        except CatalogError:
            return {}

    async def place_hold(self, title_id: str, library: str | None) -> None:
        card = await self._card(library)
        await self._request(
            "POST", f"/card/{card.id}/hold/{title_id}", json={"days_to_suspend": 0, "email_address": ""}
        )

    async def cancel_hold(self, title_id: str, library: str | None) -> None:
        card = await self._card(library)
        await self._request("DELETE", f"/card/{card.id}/hold/{title_id}")

    async def borrow(self, title_id: str, library: str | None, media_type: str, days: int | None) -> None:
        card = await self._card(library)
        await self._request("POST", f"/card/{card.id}/loan/{title_id}", json=_loan_period(card, media_type, days))

    async def renew(self, title_id: str, library: str | None, media_type: str, days: int | None) -> None:
        card = await self._card(library)
        await self._request("PUT", f"/card/{card.id}/loan/{title_id}", json=_loan_period(card, media_type, days))

    async def return_loan(self, title_id: str, library: str | None) -> None:
        card = await self._card(library)
        await self._request("DELETE", f"/card/{card.id}/loan/{title_id}")

    async def _card(self, library: str | None) -> Card:
        cards = await self.cards()
        if not cards:
            raise NotConnected
        if library is None and len(cards) == 1:
            return cards[0]
        for card in cards:
            if card.library == (library or "").strip().lower():
                return card
        keys = ", ".join(card.library for card in cards)
        raise AccountError(f"Pass `library` as one of the linked libraries: {keys}.")

    async def _sync(self) -> dict[str, Any]:
        if not self._load_token():
            self._cards = []
            raise NotConnected
        if not self._refreshed:
            await self._refresh_token()  # extends the 7-day expiry of a saved token
        data = await self._request("GET", "/chip/sync")
        self._cards = await self._to_cards(data.get("cards") or [])
        if not self._cards:
            raise NotConnected
        return data

    async def _to_cards(self, items: list[dict[str, Any]]) -> list[Card]:
        # Cards usually carry the library key; fall back to the catalog's website id lookup.
        missing = [c["library"]["websiteId"] for c in items if not c.get("advantageKey") and c.get("library")]
        keys: dict[str, str] = {}
        if missing:
            try:
                keys = await self._catalog.library_keys(missing)
            except CatalogError:
                keys = {}
        cards = []
        for item in items:
            library = item.get("library") or {}
            key = item.get("advantageKey") or keys.get(str(library.get("websiteId")), "unknown")
            cards.append(
                Card(
                    id=str(item["cardId"]),
                    library=key.lower(),
                    name=item.get("cardName"),
                    library_name=library.get("name"),
                    loan_days=_loan_days(item.get("lendingPeriods") or {}),
                )
            )
        return cards

    async def _refresh_token(self, *, new: bool = False, keep_identity: bool = True) -> None:
        """Get a new token (`new`) or refresh the current one, then save it.

        Libby answers a refresh it can't honour with a fresh, unlinked identity rather than an
        error, so a changed identity id means the account link was lost.
        """
        if new:
            self._token = self._chip = None
        else:
            self._load_token()
        previous = self._chip
        data = await self._request("POST", "/chip", params={"client": "dewey"})
        self._token, self._chip = data["identity"], data.get("chip")
        self._refreshed = True
        self._save_token()
        if keep_identity and previous and self._chip != previous:
            self._cards = []
            raise NotConnected

    def _load_token(self) -> str | None:
        if self._token is None and self._path and self._path.is_file():
            try:
                saved = json.loads(self._path.read_text())
                self._token, self._chip = saved.get("identity"), saved.get("chip")
            except (OSError, ValueError):
                self._token = self._chip = None
        return self._token

    def _save_token(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"identity": self._token, "chip": self._chip}))
        self._path.chmod(0o600)

    async def _request(self, method: str, path: str, *, retry: bool = True, **kwargs: Any) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            url = path if path.startswith("https://") else f"{SENTRY_URL}{path}"
            response = await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise AccountError(f"Libby unreachable: {exc}") from exc
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code in {401, 403}:
            # The token expired or was revoked: refresh it once and retry.
            if retry and self._token and path != "/chip":
                await self._refresh_token()
                return await self._request(method, path, retry=False, **kwargs)
            raise NotConnected
        if response.is_error:
            raise AccountError(_error_message(response.status_code, body))
        return body


def _loan_period(card: Card, media_type: str, days: int | None) -> dict[str, Any]:
    days = days or card.loan_days.get(media_type) or DEFAULT_LOAN_DAYS
    return {"period": days, "units": "days", "lucky_day": None, "title_format": media_type}


def _loan_days(periods: dict[str, Any]) -> dict[str, int]:
    """Preferred loan length per format from a card's lendingPeriods ("book" means ebook)."""
    days = {}
    for kind, period in periods.items():
        amount, unit = (period.get("preference") or [None, None])[:2]
        if isinstance(amount, int) and unit == "days":
            days["ebook" if kind == "book" else kind] = amount
    return days


def _loan(item: dict[str, Any], library: dict[str, str]) -> Loan:
    return Loan(
        title_id=str(item["id"]),
        title=item.get("title") or "",
        author=item.get("firstCreatorName"),
        media_type=(item.get("type") or {}).get("id"),
        library=library.get(str(item.get("cardId")), "unknown"),
        checkout_date=item.get("checkoutDate"),
        expires=item.get("expireDate") or item.get("expires"),
        renewable_on=item.get("renewableOn"),
        libby_url=SHARE_URL.format(id=item["id"]),
    )


def _hold(item: dict[str, Any], library: dict[str, str]) -> Hold:
    return Hold(
        title_id=str(item["id"]),
        title=item.get("title") or "",
        author=item.get("firstCreatorName"),
        media_type=(item.get("type") or {}).get("id"),
        library=library.get(str(item.get("cardId")), "unknown"),
        placed=item.get("placedDate"),
        ready=bool(item.get("isAvailable")),
        position=item.get("holdListPosition"),
        estimated_wait_days=item.get("estimatedWaitDays"),
        auto_borrow=bool(item.get("autoBorrow")),
        libby_url=SHARE_URL.format(id=item["id"]),
    )


def _history(
    tagging: dict[str, Any], title: dict[str, Any] | None, keys: dict[str, str], tags: dict[str, list[str]]
) -> HistoryItem:
    title_id = str(tagging["titleId"])
    created = tagging.get("createTime")
    media_type = tagging.get("titleFormat")
    return HistoryItem(
        title_id=title_id,
        title=(title or {}).get("title") or tagging.get("sortTitle") or "",
        author=(title or {}).get("firstCreatorName") or tagging.get("sortAuthor"),
        media_type="ebook" if media_type == "book" else media_type,
        library=keys.get(str(tagging.get("websiteId")), "unknown"),
        borrowed=datetime.fromtimestamp(created / 1000, UTC).isoformat() if created else None,
        subjects=[s["name"] for s in tagging.get("titleSubjects") or []],
        tags=tags.get(title_id, []),
        libby_url=SHARE_URL.format(id=title_id),
    )


def _error_message(status: int, body: dict[str, Any]) -> str:
    upstream = body.get("upstream") or {}
    detail = upstream.get("userExplanation") or upstream.get("errorCode") or body.get("result") or body.get("message")
    return f"Libby returned {status}" + (f": {detail}" if detail else "")
