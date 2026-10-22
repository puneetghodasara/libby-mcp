from typing import Any

import httpx
import pytest
from conftest import item, mock_catalog
from mcp import Client

from libby_mcp.catalog import Catalog, CatalogError

pytestmark = pytest.mark.anyio

V2 = "/v2/libraries"


async def call(server, tool: str, **args: Any) -> tuple[bool, Any]:
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    body = result.structured_content
    if body is None:
        body = " ".join(c.text for c in result.content)
    return result.is_error, body


def availability(*items: dict | None) -> dict:
    return {"items": list(items)}


async def test_lists_read_only_tools(make_server):
    async with Client(make_server({})) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {
        "find_libraries",
        "search_titles",
        "get_availability",
        "get_title",
        "list_subjects",
    }
    assert all(t.annotations.read_only_hint for t in tools)


async def test_search_merges_libraries_by_title_and_rank(make_server):
    routes = {
        f"{V2}/spl/media": {"items": [item("1", "One"), item("2", "Two")]},
        f"{V2}/burnaby/media": {"items": [item("2", "Two"), item("3", "Three")]},
        f"{V2}/spl/media/availability": availability(item("1", ""), item("2", "")),
        f"{V2}/burnaby/media/availability": availability(
            None, item("2", "", isAvailable=False, availableCopies=0, holdsCount=5, estimatedWaitDays=30), item("3", "")
        ),
    }
    error, body = await call(make_server(routes, ["spl", "burnaby"]), "search_titles", query="x")

    assert not error
    assert [t["id"] for t in body["titles"]] == ["1", "2", "3"]
    two = body["titles"][1]
    assert [a["library"] for a in two["availability"]] == ["spl", "burnaby"]
    assert {"available": False, "holds": 5, "estimated_wait_days": 30}.items() <= two["availability"][1].items()
    assert two["authors"] == ["Ann Author"] and two["narrators"] == ["Ned Narrator"]
    assert two["libby_url"] == "https://share.libbyapp.com/title/2"


async def test_search_merges_editions_of_the_same_book(make_server):
    us = item("10", "Sapiens", publisher={"name": "HarperCollins"})
    ca = item("20", "Sapiens!", publisher={"name": "McClelland & Stewart"}, isAvailable=False, availableCopies=0)
    routes = {
        f"{V2}/spl/media": {"items": [us]},
        f"{V2}/burnaby/media": {"items": [ca]},
        f"{V2}/spl/media/availability": availability(us, None),
        f"{V2}/burnaby/media/availability": availability(None, ca),
    }
    error, body = await call(make_server(routes, ["spl", "burnaby"]), "search_titles", query="sapiens")

    assert not error
    [sapiens] = body["titles"]
    assert [(a["library"], a["title_id"], a["available"]) for a in sapiens["availability"]] == [
        ("spl", "10", True),
        ("burnaby", "20", False),
    ]


async def test_search_passes_filters(make_server):
    calls: list[httpx.Request] = []
    routes = {f"{V2}/spl/media": {"items": []}}
    await call(
        make_server(routes, ["spl"], calls),
        "search_titles",
        media_type="audiobook",
        available_only=True,
        subject_ids=["36", "80"],
        sort="newest",
        limit=5,
    )
    params = calls[0].url.params
    assert params["mediaTypes"] == "audiobook"
    assert params["showOnlyAvailable"] == "true"
    assert params.get_list("subject") == ["36", "80"]
    assert params["sortBy"] == "newlyadded"
    assert params["perPage"] == "5"
    assert params["language"] == "en"
    assert "query" not in params


async def test_search_reports_failed_library_and_keeps_others(make_server):
    routes = {
        f"{V2}/spl/media": {"items": [item("1", "One")]},
        f"{V2}/spl/media/availability": availability(item("1", "")),
    }
    error, body = await call(make_server(routes, ["spl", "nope"]), "search_titles", query="x")

    assert not error
    assert [t["id"] for t in body["titles"]] == ["1"]
    assert "Library not found" in body["errors"]["nope"]


async def test_search_fails_when_every_library_fails(make_server):
    error, body = await call(make_server({}, ["nope"]), "search_titles", query="x")
    assert error and "nope: catalog API returned 404" in body


async def test_requires_libraries(make_server):
    error, body = await call(make_server({}), "search_titles", query="x")
    assert error and "LIBBY_LIBRARIES" in body


async def test_availability_handles_unowned_and_always_available(make_server):
    routes = {
        f"{V2}/spl/media/availability": availability(
            None,
            item("7", "", availabilityType="always", availableCopies=None, ownedCopies=None),
            item("8", "", isOwned=False),
        ),
    }
    error, body = await call(make_server(routes, ["spl"]), "get_availability", title_ids=["6", "7", "8"])

    assert not error
    assert body["availability"]["6"] == [] and body["availability"]["8"] == []
    [always] = body["availability"]["7"]
    assert always["always_available"] and always["available_copies"] is None


async def test_get_title_returns_plain_text_details(make_server):
    routes = {
        "/v2/media/42": item(
            "42",
            "Sapiens",
            description="<p><b>Big</b> &amp; bold</p>",
            publisher={"name": "Harper"},
            detailedSeries={"seriesName": "Saga", "readingOrder": "2"},
        )
    }
    error, body = await call(make_server(routes), "get_title", title_id="42")

    assert not error
    assert body["description"] == "Big & bold"
    assert body["publisher"] == "Harper"
    assert body["series"] == "Saga" and body["series_position"] == "2"
    assert body["isbns"] == ["9780000000001"]
    assert body["published"] == "2020-01-02"


async def test_get_title_rejects_non_numeric_id(make_server):
    error, _ = await call(make_server({}), "get_title", title_id="abc")
    assert error


async def test_find_libraries_resolves_keys(make_server):
    def libraries(request: httpx.Request) -> dict:
        assert request.url.params["websiteIds"] == "100117"
        return {"items": [{"preferredKey": "burnaby", "name": "Burnaby Public Library", "websiteId": 100117}]}

    routes = {
        "/autocomplete/burnaby": {
            "branches": [
                {"city": "Burnaby", "systems": [{"websiteId": 100117}]},
                {"city": "Burnaby", "systems": [{"websiteId": 100117}]},
            ]
        },
        f"{V2}": libraries,
    }
    error, body = await call(make_server(routes), "find_libraries", query="burnaby")

    assert not error
    assert body["result"] == [
        {"key": "burnaby", "name": "Burnaby Public Library", "website_id": 100117, "locations": ["Burnaby"]}
    ]


async def test_list_subjects_uses_first_configured_library(make_server):
    routes = {
        f"{V2}/spl/media": {"facets": {"subjects": {"items": [{"id": "36", "name": "History", "totalItems": 9}]}}}
    }
    error, body = await call(make_server(routes, ["spl", "burnaby"]), "list_subjects")

    assert not error
    assert body["result"] == [{"id": "36", "name": "History", "count": 9}]


async def test_find_libraries_escapes_query():
    calls: list[httpx.Request] = []
    catalog = mock_catalog({"/autocomplete/new york/ny": {"branches": []}}, calls)

    assert await catalog.find_libraries("new york/ny") == []
    assert calls[0].url.raw_path == b"/autocomplete/new%20york%2Fny"


async def test_catalog_rejects_non_json():
    def html(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    catalog = Catalog(httpx.AsyncClient(transport=httpx.MockTransport(html)), backoff=0)
    with pytest.raises(CatalogError, match="invalid JSON"):
        await catalog.title("1")


async def test_catalog_retries_transient_errors():
    attempts = iter([(503, {}), (200, {"items": []})])
    calls: list[httpx.Request] = []
    catalog = mock_catalog({f"{V2}/spl/media": lambda _: next(attempts)}, calls)

    assert await catalog.search("spl", query="x") == []
    assert len(calls) == 2
