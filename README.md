# libby-mcp

An [MCP](https://modelcontextprotocol.io) server for **Libby / OverDrive**. Search library catalogs and
compare ebook and audiobook availability across several libraries without a login, and, once you link
your Libby account, see and manage your loans and holds.

> Unofficial. The catalog tools use the public catalog API behind the Libby app, which OverDrive can
> change at any time; a nightly integration test watches for that.
>
> **The account tools use Libby's private account API. OverDrive's terms prohibit other clients from
> using it and say they may suspend accounts. Use them at your own risk.**

## Tools

### Catalog (no login, read-only)

| Tool | What it does |
|---|---|
| `find_libraries` | Find Libby libraries by name, city or postal code and get their keys (e.g. `spl`). |
| `search_titles` | Search or browse one or more libraries with availability at each. Filters: format, available now, language, subjects. Sorts by relevance, popular or newest. |
| `get_availability` | Copies, holds and estimated wait for specific editions (title ids) at each library. |
| `get_title` | Full details: description, series, narrators, duration, publisher, ISBNs. |
| `list_subjects` | A library's subjects (genres) with ids for `search_titles`. |

**Editions differ by country.** Libraries often own different editions of the same book: Sapiens is
`3255516` (HarperCollins) at Seattle and `3461405` (McClelland & Stewart) at Burnaby. `search_titles`
merges editions by title, first author and format, and each availability entry carries its library's
own `title_id`.

### Account (your Libby account)

| Tool | What it does |
|---|---|
| `connect_account` | Link your account with an 8-digit setup code (Libby menu > **Copy To Another Device**). |
| `get_account` | Your library cards, loans (due and renewal dates) and holds (queue position, estimated wait). |
| `get_history` | Everything you have borrowed in Libby, most recent first, as data: borrow count and dates, status (on loan, on hold, returned), time spent, sessions, reading-position marker, audiobook length, share actually heard, pace, and your other Libby tags such as "Finished". |
| `place_hold` / `cancel_hold` | Place or cancel a hold. |
| `borrow` / `renew_loan` / `return_loan` | Borrow an available title or ready hold, renew or return a loan. |

Write tools take the library key of the card to use (optional with one card). Once linked, searches
default to your cards' libraries. Set `LIBBY_READ_ONLY=true` to hide the write tools.

**Where the login lives.** `connect_account` saves the account token to
`~/.config/libby-mcp/identity.json` (mode 600) on the machine running the server; it is never returned
to the agent. Tokens expire after 7 days unless used, and the server refreshes them on use. In temporary
environments such as cloud sessions, the file disappears with the container, so link again each session.

The account tools were checked against a real linked account (linking, cards, loans, holds, history);
write tools are tested against mocked responses only.

## Setup

Requires [uv](https://docs.astral.sh/uv/). No configuration is needed.

**Claude Code** (`.mcp.json` in your project):

```json
{
  "mcpServers": {
    "libby": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/puneetghodasara/libby-mcp", "libby-mcp"]
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`) takes the same `mcpServers` block.

**Choosing libraries.** Every tool takes one library key (`"spl"`) or several (`["spl", "burnaby"]`) in
`libraries`. When none is given, the agent asks which library you use and looks it up with
`find_libraries`. To skip that question, set optional defaults in the server's `env`, e.g.
`"LIBBY_LIBRARIES": "spl,burnaby"`; a `libraries` argument on a call still overrides them.

## Development

```bash
uv sync
uv run pytest              # unit tests (HTTP mocked)
uv run pytest -m live      # integration tests against the real API
uv run ruff check . && uv run ruff format --check .
```

## License

MIT
