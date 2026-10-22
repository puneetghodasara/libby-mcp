"""Integration tests against the real OverDrive API. Run with `pytest -m live`.

They check the API still has the shape we depend on, so failures here usually mean
OverDrive changed something.
"""

import pytest
from mcp import Client

from libby_mcp.account import Account, AccountError
from libby_mcp.catalog import Catalog
from libby_mcp.server import create_server, http_client

pytestmark = [pytest.mark.anyio, pytest.mark.live]

SAPIENS_AUDIOBOOK = "3255516"


@pytest.fixture
async def client():
    async with http_client() as http, Client(create_server(Catalog(http), ["spl", "burnaby"])) as c:
        yield c


async def call(client: Client, tool: str, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


async def test_find_libraries(client):
    body = await call(client, "find_libraries", query="seattle")
    assert any(lib["key"] == "spl" for lib in body["result"])


async def test_search_across_libraries(client):
    body = await call(client, "search_titles", query="sapiens harari", media_type="audiobook", limit=5)
    assert body["errors"] == {}
    sapiens = next(t for t in body["titles"] if t["title"] == "Sapiens")
    assert "Yuval Noah Harari" in sapiens["authors"]
    # The US and Canadian libraries own different editions; both must be merged into one result.
    editions = {a["library"]: a["title_id"] for a in sapiens["availability"]}
    assert editions.keys() == {"spl", "burnaby"}
    assert editions["spl"] == SAPIENS_AUDIOBOOK


async def test_browse_popular(client):
    body = await call(client, "search_titles", sort="popular", media_type="ebook", limit=3)
    assert body["titles"]


async def test_get_availability(client):
    body = await call(client, "get_availability", title_ids=[SAPIENS_AUDIOBOOK])
    [spl] = [a for a in body["availability"][SAPIENS_AUDIOBOOK] if a["library"] == "spl"]
    assert spl["owned_copies"] > 0


async def test_get_title(client):
    body = await call(client, "get_title", title_id=SAPIENS_AUDIOBOOK)
    assert body["title"] == "Sapiens" and body["duration"] and body["description"]


async def test_list_subjects(client):
    body = await call(client, "list_subjects", library="spl")
    assert any(s["name"] == "History" for s in body["result"])


async def test_account_rejects_invalid_setup_code(tmp_path):
    async with http_client() as http:
        catalog = Catalog(http)
        account = Account(http, catalog, tmp_path / "identity.json")
        with pytest.raises(AccountError, match="didn't accept the setup code"):
            await account.connect("00000000")
        assert await account.cards() == []


async def test_search_by_category(client):
    body = await call(client, "search_titles", libraries="spl", category_codes=["POL062000"], limit=5)
    assert body["titles"]
    assert all(any(c["code"] == "POL062000" for c in t["categories"]) for t in body["titles"])
