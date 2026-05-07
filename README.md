# CERN Library Catalogue MCP Server

An MCP server that exposes the [CERN Library Catalogue](https://catalogue.library.cern) to AI assistants.

## Tools

| Tool | Description |
|------|-------------|
| `search_literature` | Full-text search with filters: document type, language, CERN tag, CERN experiment, sort, pagination |
| `get_literature` | Fetch full details for a single record by its PID |
| `search_series` | Search journal series, periodicals, and multipart monographs |
| `get_series` | Fetch a single series record by PID |
| `lookup_by_identifier` | Look up literature by ISBN, DOI, or report number |

### `search_literature` filters

| Parameter | Values | Description |
|-----------|--------|-------------|
| `document_type` | `BOOK`, `SERIAL`, `PROCEEDINGS`, `STANDARD` | Filter by type |
| `language` | ISO 639-2 code (`eng`, `fre`, `ger`…) | Filter by language |
| `tag` | `THESIS`, `YELLOW_REPORT`, `BOOKSHOP`, `DIDACTIC_LIBRARY`… | CERN library tags |
| `experiment` | `ATLAS`, `CMS`, `ALICE`, `LHCb`… | CERN experiment association |
| `sort` | `bestmatch` (default), `mostrecent` | Sort order |

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
# Install dependencies and create the virtual environment
uv sync
```

## Running

```bash
uv run python server.py
# or via the installed script
uv run cern-library-mcp
```

## Claude Desktop / Claude Code Configuration

Add to your `claude_desktop_config.json`:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "cern-library": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/library-mcp",
        "run", "cern-library-mcp"
      ]
    }
  }
}
```

## Example queries

- *"Search the CERN library for books on quantum field theory"*
- *"Find CERN Yellow Reports about the LHC"*
- *"Show me ATLAS experiment related proceedings"*
- *"Find doctoral theses on dark matter at CERN"*
- *"Look up ISBN 9783030803948"*
- *"Search for the 'Lecture Notes in Physics' series"*
- *"Are there any English-language books on superconductivity available for loan?"*

## API Notes

- No authentication required for read operations
- Built on [InvenioILS](https://invenioils.docs.cern.ch/) (Elasticsearch-based)
- Query syntax supports field qualifiers: `title:higgs AND authors:atlas`
- Tag values and experiment names are case-insensitive in the UI but sent uppercase to the API
