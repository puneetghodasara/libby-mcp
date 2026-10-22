"""MCP server exposing the Libby / OverDrive public catalog."""

from __future__ import annotations

import asyncio
import os
from importlib.metadata import version
from typing import Annotated

import anyio
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .catalog import Catalog, CatalogError, to_title, work_key
from .models import (
    Availability,
    AvailabilityResult,
    Library,
    MediaType,
    SearchResult,
    Sort,
    Subject,
    TitleDetails,
)

INSTRUCTIONS = """\
Search Libby (OverDrive) library catalogs and check ebook/audiobook availability. No login.
Libraries are identified by a key such as 'spl'; use find_libraries to look one up.
search_titles merges editions of the same book across libraries; libraries in different countries
often own different editions, so each availability entry names that library's own title_id.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LibraryKeys = Annotated[
    list[str] | None,
    Field(description="Library keys, e.g. ['spl', 'burnaby']. Defaults to LIBBY_LIBRARIES."),
]


def default_libraries() -> list[str]:
    return [key.strip() for key in os.environ.get("LIBBY_LIBRARIES", "").split(",") if key.strip()]


def create_server(catalog: Catalog, libraries: list[str] | None = None) -> MCPServer:
    defaults = libraries or []
    server = MCPServer("libby", instructions=INSTRUCTIONS, version=version("libby-mcp"))

    def resolve(keys: list[str] | None) -> list[str]:
        keys = list(dict.fromkeys(keys or defaults))
        if not keys:
            raise ToolError(
                "No libraries given. Pass `libraries` or set LIBBY_LIBRARIES; find keys with find_libraries."
            )
        return keys

    @server.tool(annotations=READ_ONLY)
    async def find_libraries(
        query: Annotated[str, Field(description="Library name, city or postal code.", min_length=2)],
    ) -> list[Library]:
        """Find libraries that use Libby and return their keys."""
        try:
            return await catalog.find_libraries(query)
        except CatalogError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    async def search_titles(
        query: Annotated[str | None, Field(description="Title, author, series or keywords. Omit to browse.")] = None,
        libraries: LibraryKeys = None,
        media_type: MediaType | None = None,
        available_only: Annotated[bool, Field(description="Only titles available now somewhere.")] = False,
        language: Annotated[str | None, Field(description="ISO 639-1 code; null for any.")] = "en",
        subject_ids: Annotated[list[str] | None, Field(description="Subject ids from list_subjects.")] = None,
        sort: Annotated[Sort, Field(description="'popular' and 'newest' rank within each library.")] = "relevance",
        limit: Annotated[int, Field(ge=1, le=50, description="Results per library.")] = 10,
    ) -> SearchResult:
        """Search one or more library catalogs, with availability at each library.

        Editions of the same book (title, first author and format) are merged into one result,
        ranked by its best position in any library.
        """
        keys = resolve(libraries)
        found = await asyncio.gather(
            *(
                catalog.search(
                    key,
                    query=query,
                    media_type=media_type,
                    available_only=available_only,
                    language=language,
                    subject_ids=subject_ids,
                    sort=sort,
                    limit=limit,
                )
                for key in keys
            ),
            return_exceptions=True,
        )
        errors: dict[str, str] = {}
        works: dict[tuple[str, str, str], dict] = {}
        editions: dict[tuple[str, str, str], dict[str, None]] = {}
        rank: dict[tuple[str, str, str], int] = {}
        for key, result in zip(keys, found, strict=True):
            if isinstance(result, BaseException):
                errors[key] = _reason(result)
                continue
            for position, item in enumerate(result):
                work = work_key(item)
                works.setdefault(work, item)
                editions.setdefault(work, {})[str(item["id"])] = None
                rank[work] = min(rank.get(work, position), position)
        if not works and errors:
            raise ToolError("; ".join(f"{k}: {v}" for k, v in errors.items()))

        order = sorted(works, key=rank.__getitem__)
        ids = list(dict.fromkeys(i for work in order for i in editions[work]))
        availability = await _availability(catalog, keys, ids, errors)
        titles = []
        for work in order:
            entries = [entry for i in editions[work] for entry in availability.get(i, [])]
            titles.append(to_title(works[work], sorted(entries, key=lambda e: keys.index(e.library))))
        return SearchResult(titles=titles, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def get_availability(
        title_ids: Annotated[
            list[str], Field(min_length=1, max_length=50, description="OverDrive title ids (edition-specific).")
        ],
        libraries: LibraryKeys = None,
    ) -> AvailabilityResult:
        """Current copies, holds and estimated wait for specific editions at each library.

        A library missing from a title's list doesn't own that edition; use search_titles to
        find other editions of the same book.
        """
        keys = resolve(libraries)
        errors: dict[str, str] = {}
        availability = await _availability(catalog, keys, list(dict.fromkeys(title_ids)), errors)
        if len(errors) == len(keys):
            raise ToolError("; ".join(f"{k}: {v}" for k, v in errors.items()))
        return AvailabilityResult(availability=availability, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def get_title(
        title_id: Annotated[str, Field(description="OverDrive title id.", pattern=r"^\d+$")],
    ) -> TitleDetails:
        """Full details for one title: description, series, narrators, publisher, ISBNs."""
        try:
            return await catalog.title(title_id)
        except CatalogError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    async def list_subjects(
        library: Annotated[str | None, Field(description="Library key. Defaults to the first configured one.")] = None,
        media_type: MediaType | None = None,
    ) -> list[Subject]:
        """Subjects (genres) in a library's collection, largest first. Use the ids with search_titles."""
        try:
            return await catalog.subjects(resolve([library] if library else None)[0], media_type)
        except CatalogError as exc:
            raise ToolError(str(exc)) from exc

    return server


async def _availability(
    catalog: Catalog, keys: list[str], ids: list[str], errors: dict[str, str]
) -> dict[str, list[Availability]]:
    """Availability per title id across libraries; failures are recorded in `errors`."""
    if not ids:
        return {}
    results = await asyncio.gather(*(catalog.availability(key, ids) for key in keys), return_exceptions=True)
    merged: dict[str, list[Availability]] = {i: [] for i in ids}
    for key, result in zip(keys, results, strict=True):
        if isinstance(result, BaseException):
            errors.setdefault(key, _reason(result))
            continue
        for title_id, entry in result.items():
            merged.setdefault(title_id, []).append(entry)
    return merged


def _reason(exc: BaseException) -> str:
    """Message for an expected catalog failure; anything else is a bug and is re-raised."""
    if isinstance(exc, CatalogError):
        return str(exc)
    raise exc


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=20, headers={"User-Agent": f"libby-mcp/{version('libby-mcp')}"}, follow_redirects=True
    )


async def serve() -> None:
    async with http_client() as http:
        await create_server(Catalog(http), default_libraries()).run_stdio_async()


def main() -> None:
    anyio.run(serve)
