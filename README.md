# libby-mcp

An [MCP](https://modelcontextprotocol.io) server for **Libby / OverDrive** library catalogs. Search titles,
browse what's popular or new, and compare ebook and audiobook availability across several libraries.
It doesn't need a library card or login.

> Unofficial. It uses the public catalog API behind the Libby app, which OverDrive can change at any
> time. A nightly integration test watches for that.

## Tools

All tools are read-only.

| Tool | What it does |
|---|---|
| `find_libraries` | Find Libby libraries by name, city or postal code and get their keys (e.g. `spl`). |
| `search_titles` | Search or browse one or more libraries with availability at each. Filters: format, available now, language, subjects. Sorts by relevance, popular or newest. |
| `get_availability` | Copies, holds and estimated wait for specific titles at each library. |
| `get_title` | Full details: description, series, narrators, duration, publisher, ISBNs. |
| `list_subjects` | A library's subjects (genres) with ids for `search_titles`. |

**Editions differ by country.** Libraries often own different editions of the same book: Sapiens is
`3255516` (HarperCollins) at Seattle and `3461405` (McClelland & Stewart) at Burnaby. `search_titles`
merges editions by title, first author and format, and each availability entry carries its library's
own `title_id`.

## Setup

Requires [uv](https://docs.astral.sh/uv/). No configuration is needed: when no library is set, the
server tells the agent to ask which library you use and look it up with `find_libraries`. Every tool
takes one library key or several in `libraries`. To skip the question, set default libraries with
`LIBBY_LIBRARIES`; a `libraries` argument on a call still overrides them.

**Claude Code** (`.mcp.json` in your project):

```json
{
  "mcpServers": {
    "libby": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/puneetghodasara/libby-mcp", "libby-mcp"],
      "env": { "LIBBY_LIBRARIES": "spl,burnaby" }
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`) takes the same `mcpServers` block.

| Variable | Default | Purpose |
|---|---|---|
| `LIBBY_LIBRARIES` | none | Optional default library keys, one (`spl`) or several (`spl,burnaby`). Without it, the agent asks you. |
| `SSL_CERT_FILE` / `SSL_CERT_DIR` | none | Extra CA certificates (e.g. a company proxy's). Added to the public roots, not replacing them. |

## Development

```bash
uv sync
uv run pytest              # unit tests (HTTP mocked)
uv run pytest -m live      # integration tests against the real API
uv run ruff check . && uv run ruff format --check .
```

## License

MIT
