"""MCP server for Libby / OverDrive: public catalog search plus the user's own loans and holds."""

from __future__ import annotations

import asyncio
import os
import ssl
from collections.abc import Awaitable
from importlib.metadata import version
from typing import Annotated

import anyio
import certifi
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .account import Account, AccountError, default_identity_path
from .catalog import Catalog, CatalogError, to_title, work_key
from .models import (
    AccountSummary,
    Availability,
    AvailabilityResult,
    Card,
    HistoryResult,
    Library,
    MediaType,
    SearchResult,
    Sort,
    Subject,
    TitleDetails,
)

INSTRUCTIONS = """\
Search Libby (OverDrive) library catalogs and check ebook/audiobook availability. No login.
Libraries are identified by a key such as 'spl'. Tools take one key or several in `libraries`.
search_titles merges editions of the same book across libraries; libraries in different countries
often own different editions, so each availability entry names that library's own title_id.
"""

NO_DEFAULTS = """\
No default libraries are configured. If the user has linked their Libby account, their cards'
libraries are used. Otherwise ask the user which library or libraries they use (a name or city),
look them up with find_libraries, confirm the match, then pass the keys in `libraries`.
"""

ACCOUNT = """\
Account tools act on the user's own Libby account and need a one-time link: ask the user for an
8-digit setup code (Libby menu > 'Copy To Another Device') and call connect_account. Confirm with the
user before borrowing, returning, renewing or changing holds. Use the title_id the chosen library
owns (from search_titles availability), since editions differ between libraries.
"""

ASK_FOR_LIBRARY = (
    "No library selected. Ask the user which library they use (name or city), look it up with "
    "find_libraries, and pass its key in `libraries`."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
TitleId = Annotated[str, Field(description="OverDrive title id the library owns.", pattern=r"^\d+$")]
CardLibrary = Annotated[
    str | None, Field(description="Library key of the card to use; optional with a single linked card.")
]
LibraryKeys = Annotated[
    list[str] | str | None,
    Field(description="One library key or several, e.g. 'spl' or ['spl', 'burnaby']. Overrides the defaults."),
]


def default_libraries() -> list[str]:
    """Optional defaults from LIBBY_LIBRARIES, e.g. 'spl' or 'spl,burnaby'."""
    return parse_libraries(os.environ.get("LIBBY_LIBRARIES"))


def parse_libraries(keys: list[str] | str | None) -> list[str]:
    """Normalise one key, a comma-separated string or a list into unique, ordered keys."""
    if isinstance(keys, str):
        keys = keys.split(",")
    return list(dict.fromkeys(key.strip().lower() for key in keys or [] if key.strip()))


def create_server(
    catalog: Catalog,
    libraries: list[str] | str | None = None,
    account: Account | None = None,
    *,
    read_only: bool = False,
) -> MCPServer:
    defaults = parse_libraries(libraries)
    configured = f"Default libraries: {', '.join(defaults)}. Omit `libraries` to use them.\n"
    instructions = INSTRUCTIONS + (configured if defaults else NO_DEFAULTS) + (ACCOUNT if account else "")
    server = MCPServer("libby", instructions=instructions, version=version("libby-mcp"))

    async def resolve(keys: list[str] | str | None) -> list[str]:
        """Explicit keys win; then configured defaults; then the linked account's cards; else ask."""
        resolved = parse_libraries(keys) or defaults
        if not resolved and account:
            resolved = list(dict.fromkeys(card.library for card in await account.cards()))
        if not resolved:
            raise ToolError(ASK_FOR_LIBRARY)
        return resolved

    @server.tool(annotations=READ_ONLY)
    async def find_libraries(
        query: Annotated[str, Field(description="Library name, city or postal code.", min_length=2)],
    ) -> list[Library]:
        """Find libraries that use Libby by name, city or postal code, and return their keys.

        Several libraries can match (branches, consortia, special collections); confirm the right
        one with the user before using its key.
        """
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
        """Search one library or several at once, with availability at each.

        Editions of the same book (title, first author and format) are merged into one result,
        ranked by its best position in any library.
        """
        keys = await resolve(libraries)
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
        keys = await resolve(libraries)
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
            return await catalog.subjects((await resolve(library))[0], media_type)
        except CatalogError as exc:
            raise ToolError(str(exc)) from exc

    if account:
        _add_account_tools(server, catalog, account, read_only=read_only)
    return server


def _add_account_tools(server: MCPServer, catalog: Catalog, account: Account, *, read_only: bool) -> None:
    @server.tool(annotations=WRITE)
    async def connect_account(
        code: Annotated[str, Field(description="8-digit setup code from Libby's 'Copy To Another Device'.")],
    ) -> list[Card]:
        """Link the user's Libby account with a one-time setup code. Returns the linked library cards."""
        try:
            return await account.connect(code)
        except AccountError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    async def get_account() -> AccountSummary:
        """The user's library cards, current loans (with due dates) and holds (with queue position)."""
        try:
            return await account.summary()
        except AccountError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    async def get_history(
        library: Annotated[str | None, Field(description="Only titles borrowed from this library key.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Most recent titles to return.")] = 100,
        progress: Annotated[bool, Field(description="Include reading progress (one lookup per title).")] = True,
    ) -> HistoryResult:
        """Titles the user has borrowed in Libby, newest first, from Libby's automatic borrowing history.

        Each title lists the user's other Libby tags (such as 'Finished') and, with `progress`, how far
        they read or listened, total time spent and when they last opened it. A borrowed title with
        progress near 0 was barely opened; near 1, finished.
        """
        try:
            return await account.history(library, limit, progress=progress)
        except AccountError as exc:
            raise ToolError(str(exc)) from exc

    if read_only:
        return

    @server.tool(annotations=WRITE)
    async def place_hold(title_id: TitleId, library: CardLibrary = None) -> str:
        """Place a hold on a title. Confirm with the user first."""
        return await _run(account.place_hold(title_id, library), f"Hold placed on {title_id}.")

    @server.tool(annotations=DESTRUCTIVE)
    async def cancel_hold(title_id: TitleId, library: CardLibrary = None) -> str:
        """Cancel a hold; the user loses their place in the queue. Confirm with the user first."""
        return await _run(account.cancel_hold(title_id, library), f"Hold on {title_id} cancelled.")

    @server.tool(annotations=WRITE)
    async def borrow(
        title_id: TitleId,
        library: CardLibrary = None,
        days: Annotated[
            int | None, Field(ge=1, le=21, description="Loan length in days; defaults to the card's preference.")
        ] = None,
    ) -> str:
        """Borrow an available title, or a hold that is ready. Confirm with the user first."""
        return await _run(
            account.borrow(title_id, library, await _media_type(catalog, title_id), days), f"Borrowed {title_id}."
        )

    @server.tool(annotations=WRITE)
    async def renew_loan(
        title_id: TitleId,
        library: CardLibrary = None,
        days: Annotated[
            int | None, Field(ge=1, le=21, description="Loan length in days; defaults to the card's preference.")
        ] = None,
    ) -> str:
        """Renew a loan (possible from its renewable_on date). Confirm with the user first."""
        return await _run(
            account.renew(title_id, library, await _media_type(catalog, title_id), days), f"Renewed {title_id}."
        )

    @server.tool(annotations=DESTRUCTIVE)
    async def return_loan(title_id: TitleId, library: CardLibrary = None) -> str:
        """Return a loan early. Confirm with the user first."""
        return await _run(account.return_loan(title_id, library), f"Returned {title_id}.")


async def _run(action: Awaitable[None], done: str) -> str:
    try:
        await action
    except AccountError as exc:
        raise ToolError(str(exc)) from exc
    return done


async def _media_type(catalog: Catalog, title_id: str) -> str:
    try:
        return (await catalog.title(title_id)).media_type
    except CatalogError as exc:
        raise ToolError(str(exc)) from exc


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


def ssl_context() -> ssl.SSLContext:
    """Public roots (certifi) plus any extra CAs from SSL_CERT_FILE / SSL_CERT_DIR.

    Extra CAs are added rather than replacing the public roots, so the server keeps working
    behind proxies whose bundle holds only their own CA or an incomplete set of roots.
    """
    context = ssl.create_default_context(cafile=certifi.where())
    cafile = os.environ.get("SSL_CERT_FILE") or ""
    capath = os.environ.get("SSL_CERT_DIR") or ""
    if os.path.isfile(cafile) or os.path.isdir(capath):
        context.load_verify_locations(
            cafile=cafile if os.path.isfile(cafile) else None, capath=capath if os.path.isdir(capath) else None
        )
    return context


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": f"libby-mcp/{version('libby-mcp')}"},
        follow_redirects=True,
        verify=ssl_context(),
    )


async def serve() -> None:
    read_only = os.environ.get("LIBBY_READ_ONLY", "").strip().lower() in {"1", "true", "yes"}
    async with http_client() as http:
        catalog = Catalog(http)
        account = Account(http, catalog, default_identity_path())
        await create_server(catalog, default_libraries(), account, read_only=read_only).run_stdio_async()


def main() -> None:
    anyio.run(serve)
