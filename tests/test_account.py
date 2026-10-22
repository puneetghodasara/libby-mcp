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
    {
        "cardId": "111",
        "advantageKey": "spl",
        "cardName": "Me",
        "library": {"websiteId": "83", "name": "Seattle"},
        "lendingPeriods": {
            "book": {"options": [[7, "days"], [21, "days"]], "preference": [21, "days"]},
            "audiobook": {"options": [[7, "days"], [14, "days"]], "preference": [14, "days"]},
        },
    },
    {"cardId": "222", "library": {"websiteId": "100117", "name": "Burnaby"}},  # no key: resolved via the catalog
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
    """POST /chip: a new token, or a refreshed one derived from the bearer token; same identity."""
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return {"identity": f"{bearer or 'new'}+", "chip": "c1"}


def routes(**overrides: Any) -> dict[str, Any]:
    return {
        "POST /chip": chip,
        "POST /chip/clone/code": {"result": "cloned"},
        "GET /chip/sync": SYNC,
        "/v2/libraries": {"items": [{"websiteId": 100117, "preferredKey": "burnaby"}]},  # int here, str in sync
        "/v2/media/42": item("42", "Sapiens", type={"id": "audiobook"}),
        **overrides,
    }


@pytest.fixture
def setup(tmp_path):
    def make(routes: dict[str, Any], *, token: str | None = None, read_only: bool = False, libraries=None):
        path = tmp_path / "identity.json"
        if token:
            path.write_text(json.dumps({"identity": token, "chip": "c1"}))
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
    assert json.loads(path.read_text()) == {"identity": "new++", "chip": "c1"}
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
    assert json.loads(path.read_text()) == {"identity": "old+", "chip": "c1"}


async def test_rejected_token_is_refreshed_and_retried(setup):
    attempts = iter([(403, {"result": "unauthorized"}), (200, SYNC)])
    server, calls, _ = setup(routes(**{"GET /chip/sync": lambda _: next(attempts)}), token="old")
    error, body = await call(server, "get_account")

    assert not error and body["loans"]
    syncs = [c.headers["Authorization"] for c in calls if c.url.path == "/chip/sync"]
    assert syncs == ["Bearer old+", "Bearer old++"]


async def test_lost_link_asks_to_reconnect(setup):
    # Libby answers a refresh it can't honour with a fresh, unlinked identity instead of an error.
    lost = {"identity": "anon", "chip": "c2"}
    server, calls, path = setup(routes(**{"POST /chip": lost}), token="stale")
    error, body = await call(server, "get_account")

    assert error and "No Libby account is linked" in body
    assert not any(c.url.path == "/chip/sync" for c in calls)
    assert json.loads(path.read_text())["chip"] == "c2"


async def test_rejected_twice_asks_to_reconnect(setup):
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


async def test_borrow_defaults_to_the_cards_preferred_length(setup):
    server, calls, _ = setup(
        routes(**{"POST /card/111/loan/42": {}, "/v2/media/42": item("42", "Sapiens", type={"id": "ebook"})}),
        token="t",
    )
    error, _ = await call(server, "borrow", title_id="42", library="spl")

    assert not error
    loan = next(c for c in calls if c.url.path == "/card/111/loan/42")
    assert json.loads(loan.content)["period"] == 21


async def test_borrow_falls_back_without_a_card_preference(setup):
    server, calls, _ = setup(routes(**{"POST /card/222/loan/42": {}}), token="t")
    error, _ = await call(server, "borrow", title_id="42", library="burnaby")

    assert not error
    loan = next(c for c in calls if c.url.path == "/card/222/loan/42")
    assert json.loads(loan.content)["period"] == 14


async def test_cards_show_preferred_loan_days(setup):
    server, _, _ = setup(routes(), token="t")
    _, body = await call(server, "get_account")
    assert body["cards"][0]["loan_days"] == {"ebook": 21, "audiobook": 14}


async def test_libby_errors_are_reported(setup):
    failure = (400, {"result": "upstream_failure", "upstream": {"userExplanation": "This title is not available."}})
    server, _, _ = setup(routes(**{"POST /card/111/loan/42": failure}), token="t")

    error, body = await call(server, "borrow", title_id="42", library="spl")
    assert error and "This title is not available." in body


async def test_read_only_hides_write_tools(setup):
    server, _, _ = setup(routes(), read_only=True)
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"connect_account", "get_account", "get_history"} <= names
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


def tagging(title_id: str, website: str, created: int, **extra: Any) -> dict:
    return {
        "titleId": title_id,
        "websiteId": website,
        "cardId": "111",
        "createTime": created,
        "titleFormat": "audiobook",
        "sortTitle": f"Sort {title_id}",
        "sortAuthor": "Author, Sort",
        "titleSubjects": [{"id": "36", "name": "History"}],
        **extra,
    }


BORROWED = [
    tagging("30", "83", 1_780_000_000_000),
    tagging("31", "100117", 1_790_000_000_000, titleFormat="book"),
    tagging("32", "83", 1_770_000_000_000),
]


def tag_routes(**overrides: Any) -> dict[str, Any]:
    tags = {
        "tags": [
            # The list endpoint previews only some taggings; the full tag has them all.
            {"name": "🧾", "uuid": "u1", "behaviors": {"borrowed": {}}, "totalTaggings": 3, "taggings": BORROWED[:1]},
            {
                "name": "Finished",
                "uuid": "u2",
                "behaviors": {},
                "totalTaggings": 1,
                "taggings": [tagging("30", "83", 1)],
            },
        ]
    }
    libraries = {"items": [{"websiteId": 83, "preferredKey": "spl"}, {"websiteId": 100117, "preferredKey": "burnaby"}]}
    return routes(
        **{
            "GET /tags": tags,
            "GET /tag/u1/🧾": {"tag": {"taggings": BORROWED}},
            "/v2/media/bulk": [item("30", "Homo Deus", firstCreatorName="Yuval Noah Harari"), item("31", "Dune")],
            "/v2/libraries": libraries,
            **overrides,
        }
    )


async def test_get_history_lists_borrowed_titles_newest_first(setup):
    server, calls, _ = setup(tag_routes(), token="t")
    error, body = await call(server, "get_history")

    assert not error
    assert body["total"] == 3
    items = body["items"]
    assert [(i["title_id"], i["library"], i["media_type"]) for i in items] == [
        ("31", "burnaby", "ebook"),
        ("30", "spl", "audiobook"),
        ("32", "spl", "audiobook"),
    ]
    assert items[1]["title"] == "Homo Deus" and items[1]["author"] == "Yuval Noah Harari"
    assert items[1]["tags"] == ["Finished"] and items[1]["borrowed"].startswith("2026-05-28")
    assert items[2]["title"] == "Sort 32"  # no longer in the catalog: falls back to the sort title
    full = next(c for c in calls if c.url.path.startswith("/tag/u1/"))
    assert full.url.raw_path == b"/tag/u1/%F0%9F%A7%BE" and full.headers["Authorization"] == "Bearer t+"


async def test_get_history_filters_by_library_and_limit(setup):
    server, _, _ = setup(tag_routes(), token="t")
    error, body = await call(server, "get_history", library="spl", limit=1)

    assert not error
    assert [i["title_id"] for i in body["items"]] == ["30"] and body["total"] == 3


async def test_get_history_without_borrowing_tag(setup):
    server, _, _ = setup(tag_routes(**{"GET /tags": {"tags": []}}), token="t")
    error, body = await call(server, "get_history")
    assert not error and body == {"items": [], "total": 0}
