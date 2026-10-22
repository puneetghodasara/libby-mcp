from collections.abc import Callable
from typing import Any

import httpx
import pytest

from libby_mcp.catalog import Catalog
from libby_mcp.server import create_server

Routes = dict[str, Any]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def item(title_id: str, title: str, **overrides: Any) -> dict[str, Any]:
    """A catalog item shaped like OverDrive's response, trimmed to the fields we read."""
    return {
        "id": title_id,
        "title": title,
        "creators": [{"name": "Ann Author", "role": "Author"}, {"name": "Ned Narrator", "role": "Narrator"}],
        "type": {"id": "audiobook"},
        "languages": [{"id": "en"}],
        "subjects": [{"id": "36", "name": "History"}],
        "publishDate": "2020-01-02T00:00:00Z",
        "formats": [{"id": "audiobook-overdrive", "duration": "10:00:00", "isbn": "9780000000001"}],
        "isAvailable": True,
        "availableCopies": 1,
        "ownedCopies": 2,
        "holdsCount": 0,
        **overrides,
    }


def mock_http(routes: Routes, calls: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """HTTP client backed by `routes`: "METHOD /path" or "/path" -> JSON body, (status, body), or a callable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        route = routes.get(f"{request.method} {path}", routes.get(path))
        if route is None:
            return httpx.Response(404, json={"message": "Library not found."})
        if callable(route):
            route = route(request)
        status, body = route if isinstance(route, tuple) else (200, route)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def mock_catalog(routes: Routes, calls: list[httpx.Request] | None = None) -> Catalog:
    return Catalog(mock_http(routes, calls), backoff=0)


@pytest.fixture
def make_server() -> Callable[..., Any]:
    def make(routes: Routes, libraries: list[str] | None = None, calls: list[httpx.Request] | None = None):
        return create_server(mock_catalog(routes, calls), libraries)

    return make
