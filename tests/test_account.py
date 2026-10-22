import json
from typing import Any

import httpx
import pytest
from conftest import item, mock_http
from mcp import Client

from libby_mcp.account import Account
from libby_mcp.catalog import Catalog
from libby_mcp.server import create_server

pytestmark = pytest.mark.anyio

CARDS = [
    {"cardId": "111", "advantageKey": "spl", "cardName": "Me", "library": {"websiteId": 83, "name": "Seattle"}},
    {"cardId": "222", "library": {"websiteId": 100117, "name": "Burnaby"}},  # no key: resolved via the catalog
]
SYNC = {
    "result": "synchronized",
    "cards": CARDS,
    "loans": [
        {
            "id": "10",
            "cardId": "111",
            "title": "Sapiens",
            "firstCreatorName": "Yuval Noah Harari",
            "type": {"id": "audiobook"},
            "checkoutDate": "2026-10-01T00:00:00Z",
            "expireDate": "2026-10-15T00:00:00Z",
            "renewableOn": "2026-10-12T00:00:00Z",
        }
    ],
    "holds": [
        {
            "id": "20",
            "cardId": "222",
            "title": "Dune",
            "type": {"id": "ebook"},
            "holdListPosition": 3,
            "estimatedWaitDays": 12,
            "isAvailable": False,
            "placedDate": "2026-10-02T00:00:00Z",
        }
    ],
}


def chip(request: httpx.Request) -> dict:
    """POST /chip: a new token, or a refreshed one derived from the bearer token."""
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return {"identity": f"{bearer or 'new'}+", "chip": "c1"}


def routes(**overrides: Any) -> dict[str, Any]:
    return {
        "POST /chip": chip,
        "POST /chip/clone/code": {"result": "cloned"},
        "GET /chip/sync": SYNC,
        "/v2/libraries": {"items": [{"websiteId": 100117, "preferredKey": "burnaby"}]},
        "/v2/media/42": item("42", "Sapiens", type={"id": "audiobook"}),
        **overrides,
    }


@pytest.fixture
def setup(tmp_path):
    def make(routes: dict[str, Any], *, token: str | None = None, read_only: bool = False, libraries=None):
        path = tmp_path / "identity.json"
        if token:
            path.write_text(json.dumps({"identity": token}))
        calls: list[httpx.Request] = []
        http = mock_http(routes, calls)
        catalog = Catalog(http, backoff=0)
        server = create_server(catalog, libraries, Account(http, catalog, path), read_only=read_only)
        return server, calls, path

    return make


async def call(server, tool: str, **args: Any) -> tuple[bool, Any]:
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    body = result.structured_content
    if body is None:
        body = " ".join(c.text for c in result.content)
    return result.is_error, body


async def test_connect_links_account_and_saves_token(setup):
    server, calls, path = setup(routes())
    error, body = await call(server, "connect_account", code="1234 5678")

    assert not error
    assert [(c["id"], c["library"]) for c in body["result"]] == [("111", "spl"), ("222", "burnaby")]
    clone = next(c for c in calls if c.url.path == "/chip/clone/code")
    assert clone.content == b"code=12345678" and clone.headers["Authorization"] == "Bearer new+"
    assert json.loads(path.read_text()) == {"identity": "new++"}
    assert path.stat().st_mode & 0o777 == 0o600


async def test_connect_rejects_bad_and_expired_codes(setup):
    server, _, _ = setup(routes(**{"POST /chip/clone/code": (404, {"result": "not_found"})}))

    error, body = await call(server, "connect_account", code="123")
    assert error and "8 digits" in body
    error, body = await call(server, "connect_account", code="12345678")
    assert error and "didn't accept the setup code" in body and "not_found" in body


async def test_not_connected_asks_for_a_setup_code(setup):
    server, _, _ = setup(routes())
    assert "Copy To Another Device" in server.instructions

    error, body = await call(server, "get_account")
    assert error and "Copy To Another Device" in body and "connect_account" in body


async def test_saved_token_is_refreshed_before_use(setup):
    server, calls, path = setup(routes(), token="old")
    error, _ = await call(server, "get_account")

    assert not error
    refresh = next(c for c in calls if c.url.path == "/chip")
    assert refresh.headers["Authorization"] == "Bearer old"
    sync = next(c for c in calls if c.url.path == "/chip/sync")
    assert sync.headers["Authorization"] == "Bearer old+"
    assert json.loads(path.read_text()) == {"identity": "old+"}


async def test_expired_token_asks_to_reconnect(setup):
    # Libby answers an invalid token with 403, and a refresh of one with a new, unlinked chip.
    server, _, _ = setup(routes(**{"GET /chip/sync": (403, {"result": "unauthorized"})}), token="stale")
    error, body = await call(server, "get_account")
    assert error and "No Libby account is linked" in body


async def test_get_account_maps_loans_and_holds_to_libraries(setup):
    server, _, _ = setup(routes(), token="t")
    error, body = await call(server, "get_account")

    assert not error
    [loan] = body["loans"]
    assert (loan["title_id"], loan["library"], loan["expires"], loan["media_type"]) == (
        "10",
        "spl",
        "2026-10-15T00:00:00Z",
        "audiobook",
    )
    [hold] = body["holds"]
    assert (hold["library"], hold["position"], hold["estimated_wait_days"], hold["ready"]) == ("burnaby", 3, 12, False)


async def test_place_and_cancel_hold_use_the_chosen_card(setup):
    server, calls, _ = setup(routes(**{"POST /card/222/hold/42": {}, "DELETE /card/222/hold/42": {}}), token="t")

    assert not (await call(server, "place_hold", title_id="42", library="burnaby"))[0]
    assert not (await call(server, "cancel_hold", title_id="42", library="Burnaby"))[0]
    place = next(c for c in calls if c.method == "POST" and c.url.path == "/card/222/hold/42")
    assert json.loads(place.content) == {"days_to_suspend": 0, "email_address": ""}


async def test_write_tools_need_a_library_when_several_cards(setup):
    server, _, _ = setup(routes(), token="t")
    error, body = await call(server, "place_hold", title_id="42")
    assert error and "spl, burnaby" in body


async def test_borrow_sends_format_and_period(setup):
    server, calls, _ = setup(routes(**{"POST /card/111/loan/42": {}}), token="t")
    error, _ = await call(server, "borrow", title_id="42", library="spl", days=7)

    assert not error
    loan = next(c for c in calls if c.url.path == "/card/111/loan/42")
    assert json.loads(loan.content) == {"period": 7, "units": "days", "lucky_day": None, "title_format": "audiobook"}


async def test_libby_errors_are_reported(setup):
    failure = (400, {"result": "upstream_failure", "upstream": {"userExplanation": "This title is not available."}})
    server, _, _ = setup(routes(**{"POST /card/111/loan/42": failure}), token="t")

    error, body = await call(server, "borrow", title_id="42", library="spl")
    assert error and "This title is not available." in body


async def test_read_only_hides_write_tools(setup):
    server, _, _ = setup(routes(), read_only=True)
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"connect_account", "get_account"} <= names
    assert not names & {"place_hold", "cancel_hold", "borrow", "renew_loan", "return_loan"}


async def test_search_defaults_to_linked_card_libraries(setup):
    server, calls, _ = setup(
        routes(**{"/v2/libraries/spl/media": {"items": []}, "/v2/libraries/burnaby/media": {"items": []}}), token="t"
    )
    error, _ = await call(server, "search_titles", query="x")

    assert not error
    assert {c.url.path for c in calls if c.url.path.endswith("/media")} == {
        "/v2/libraries/spl/media",
        "/v2/libraries/burnaby/media",
    }


async def test_explicit_defaults_win_over_linked_cards(setup):
    server, calls, _ = setup(routes(**{"/v2/libraries/kcls/media": {"items": []}}), token="t", libraries=["kcls"])
    await call(server, "search_titles", query="x")
    assert not any(c.url.path == "/chip/sync" for c in calls)
