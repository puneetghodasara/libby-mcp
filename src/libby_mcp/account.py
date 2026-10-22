"""Client for the Libby account API: link a device, then read and change loans and holds.

Linking uses Libby's "Copy To Another Device" setup code. The resulting token expires
after 7 days unless refreshed, so it is refreshed on use and saved to a local file.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .catalog import SHARE_URL, Catalog, CatalogError
from .models import AccountSummary, Card, Hold, Loan

SENTRY_URL = "https://sentry.libbyapp.com"
_CODE = re.compile(r"^\d{8}$")

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
        await self._refresh_token()
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
        await self._request("POST", f"/card/{card.id}/loan/{title_id}", json=_loan_period(media_type, days))

    async def renew(self, title_id: str, library: str | None, media_type: str, days: int | None) -> None:
        card = await self._card(library)
        await self._request("PUT", f"/card/{card.id}/loan/{title_id}", json=_loan_period(media_type, days))

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
        keys: dict[int, str] = {}
        if missing:
            try:
                keys = await self._catalog.library_keys(missing)
            except CatalogError:
                keys = {}
        cards = []
        for item in items:
            library = item.get("library") or {}
            key = item.get("advantageKey") or keys.get(library.get("websiteId"), "unknown")
            cards.append(
                Card(
                    id=str(item["cardId"]),
                    library=key.lower(),
                    name=item.get("cardName"),
                    library_name=library.get("name"),
                )
            )
        return cards

    async def _refresh_token(self, *, new: bool = False) -> None:
        """Get a new token (`new`) or refresh the current one, then save it."""
        if new:
            self._token = None
        else:
            self._load_token()
        data = await self._request("POST", "/chip", params={"client": "dewey"})
        self._token = data["identity"]
        self._refreshed = True
        self._save_token()

    def _load_token(self) -> str | None:
        if self._token is None and self._path and self._path.is_file():
            try:
                self._token = json.loads(self._path.read_text()).get("identity")
            except (OSError, ValueError):
                self._token = None
        return self._token

    def _save_token(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"identity": self._token}))
        self._path.chmod(0o600)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.request(method, f"{SENTRY_URL}{path}", headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise AccountError(f"Libby unreachable: {exc}") from exc
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code in {401, 403}:
            raise NotConnected
        if response.is_error:
            raise AccountError(_error_message(response.status_code, body))
        return body


def _loan_period(media_type: str, days: int | None) -> dict[str, Any]:
    return {"period": days or 14, "units": "days", "lucky_day": None, "title_format": media_type}


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


def _error_message(status: int, body: dict[str, Any]) -> str:
    upstream = body.get("upstream") or {}
    detail = upstream.get("userExplanation") or upstream.get("errorCode") or body.get("result")
    return f"Libby returned {status}" + (f": {detail}" if detail else "")
