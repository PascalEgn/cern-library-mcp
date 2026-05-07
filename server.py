"""
MCP server for the CERN Library Catalogue (catalogue.library.cern).

Tools:
  - search_literature    : full-text search with filters (tag, experiment, doc type, language)
  - get_literature       : fetch a single literature record by PID
  - search_series        : search journal series, periodicals, and multipart monographs
  - get_series           : fetch a single series record by PID
  - lookup_by_identifier : look up literature by ISBN, DOI, or report number
  - get_item_locations   : fetch physical copy locations + CERN map URLs for a record
  - browse_literature    : browse newest arrivals or most-loaned books/documents
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://catalogue.library.cern/api"
LITERATURE_ENDPOINT = f"{BASE_URL}/literature/"
DOCUMENTS_ENDPOINT = f"{BASE_URL}/documents/"
SERIES_ENDPOINT = f"{BASE_URL}/series/"

BROWSE_SORT_MAP = {
    "newest": "-created",
    "most_loaned": "-mostloaned",
    "recently_published": "-mostrecent",
}
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
REQUEST_TIMEOUT = 15.0

# Valid tag values exposed by the catalogue aggregations
VALID_TAGS = [
    "THESIS",
    "YELLOW_REPORT",
    "BOOKSHOP",
    "DIDACTIC_LIBRARY",
    "LEGAL_SERVICE_LIBRARY",
    "OMBUD_LIBRARY",
    "DIVERSITY_AND_INCLUSION",
    "WOMEN_IN_TECHNOLOGY",
    "DRO",
]

# Known CERN experiments (extensions.unit_experiment field)
KNOWN_EXPERIMENTS = ["ATLAS", "CMS", "ALICE", "LHCb", "NA62", "NA64", "TOTEM", "LHCf", "MoEDAL"]

# ---------------------------------------------------------------------------
# HTTP client (shared, connection-pooled)
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    return _http_client


# ---------------------------------------------------------------------------
# Shared record fetcher (literature endpoint with documents fallback)
# ---------------------------------------------------------------------------


async def _fetch_record(pid: str) -> dict[str, Any]:
    """Fetch a single record, trying /literature/ then /documents/ as fallback."""
    client = get_http_client()
    for endpoint in (f"{BASE_URL}/literature/{pid}", f"{BASE_URL}/documents/{pid}"):
        response = await client.get(endpoint)
        if response.status_code == 200:
            return response.json()
    response.raise_for_status()
    return {}  # unreachable


# ---------------------------------------------------------------------------
# Map URL builder
# ---------------------------------------------------------------------------


def _build_map_url(shelves: list[str | int], scale: int = 200) -> str:
    """Return a maps.web.cern.ch URL highlighting the given shelf numbers."""
    names = [f"SHELF {s}" for s in shelves]
    n_param = "[" + ",".join(f"'{name}'" for name in names) + "]"
    qs = urllib.parse.urlencode(
        {"n": n_param, "showMenu": "false", "widgets": "", "scale": scale},
        quote_via=urllib.parse.quote,
    )
    return f"https://maps.web.cern.ch/?{qs}"


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _append_filter(q: str, clause: str) -> str:
    return f"({q}) AND {clause}" if q.strip() else clause


def _build_literature_params(
    query: str,
    sort: str,
    page: int,
    size: int,
    doc_type: str | None,
    language: str | None,
    tag: str | None,
    experiment: str | None,
) -> dict[str, Any]:
    q = query
    if doc_type:
        q = _append_filter(q, f"document_type:{doc_type.upper()}")
    if language:
        q = _append_filter(q, f"languages:{language.lower()}")
    if tag:
        q = _append_filter(q, f"tags:{tag.upper()}")
    if experiment:
        q = _append_filter(q, f"extensions.unit_experiment:{experiment.upper()}")
    return {
        "q": q,
        "sort": sort,
        "page": page,
        "size": min(size, MAX_PAGE_SIZE),
    }


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _extract_identifiers(meta: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for ident in meta.get("identifiers", []):
        scheme = ident.get("scheme", "OTHER")
        value = ident.get("value")
        if value:
            result.setdefault(scheme, []).append(value)
    return result


def _format_hit(hit: dict[str, Any]) -> dict[str, Any]:
    meta = hit.get("metadata", {})

    authors = [
        a.get("full_name", "") for a in meta.get("authors", []) if a.get("full_name")
    ]

    eitems = meta.get("eitems", {}).get("hits", [])
    online_urls = [
        url.get("value")
        for eitem in eitems
        for url in eitem.get("metadata", {}).get("urls", [])
        if url.get("value")
    ]

    identifiers = _extract_identifiers(meta)

    # Conference info
    conf = meta.get("conference_info") or {}
    if isinstance(conf, list):
        conf = conf[0] if conf else {}
    conference = None
    if conf.get("title"):
        conference = {
            "title": conf.get("title"),
            "dates": conf.get("dates"),
            "place": conf.get("place"),
            "year": conf.get("year"),
            "identifier": conf.get("identifiers", [{}])[0].get("value") if conf.get("identifiers") else None,
        }

    # Extensions (CERN-specific: experiment, accelerator, project)
    ext = meta.get("extensions") or {}
    extensions = None
    if ext:
        extensions = {
            "accelerator": ext.get("unit_accelerator"),
            "experiment": ext.get("unit_experiment") or [],
            "project": ext.get("unit_project") or [],
        }

    # Relations
    relations: dict[str, Any] = {}
    for rel_type, rel_list in (meta.get("relations") or {}).items():
        if rel_list:
            relations[rel_type] = [
                {"pid": r.get("pid"), "title": r.get("title")}
                for r in (rel_list if isinstance(rel_list, list) else [rel_list])
            ]

    # Subjects
    subjects = [
        {"scheme": s.get("scheme"), "value": s.get("value")}
        for s in meta.get("subjects", [])
        if s.get("value")
    ]

    # Cover image
    cover = meta.get("cover_metadata", {})
    cover_url = cover.get("urls", {}).get("medium") or cover.get("medium")

    pid = meta.get("pid")
    return {
        "pid": pid,
        "title": meta.get("title"),
        "authors": authors,
        "document_type": meta.get("document_type"),
        "publication_year": meta.get("publication_year"),
        "languages": meta.get("languages", []),
        "abstract": meta.get("abstract"),
        "identifiers": identifiers,
        "keywords": [k.get("value") for k in meta.get("keywords", []) if k.get("value")],
        "tags": meta.get("tags", []),
        "extensions": extensions,
        "subjects": subjects or None,
        "conference": conference,
        "relations": relations or None,
        "table_of_contents": meta.get("table_of_content") or None,
        "number_of_pages": meta.get("number_of_pages"),
        "available_copies": meta.get("circulation", {}).get("available_items_for_loan_count"),
        "online_urls": online_urls,
        "cover_url": cover_url,
        "catalogue_url": f"https://catalogue.library.cern/literature/{pid}",
    }


def _format_series_hit(hit: dict[str, Any]) -> dict[str, Any]:
    meta = hit.get("metadata", {})
    identifiers = _extract_identifiers(meta)
    pid = meta.get("pid")
    return {
        "pid": pid,
        "title": meta.get("title"),
        "series_type": meta.get("series_type"),
        "mode_of_issuance": meta.get("mode_of_issuance"),
        "identifiers": identifiers,
        "languages": meta.get("languages", []),
        "abbreviation": meta.get("abbreviation"),
        "catalogue_url": f"https://catalogue.library.cern/series/{pid}",
    }


def _format_search_response(data: dict[str, Any], formatter: Any) -> dict[str, Any]:
    hits = data.get("hits", {})
    return {
        "total": hits.get("total", 0),
        "results": [formatter(h) for h in hits.get("hits", [])],
        "links": data.get("links", {}),
        "aggregations": {
            key: [b["key"] for b in bucket_data.get("buckets", [])]
            for key, bucket_data in data.get("aggregations", {}).items()
            if bucket_data.get("buckets")
        },
    }


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def handle_search_literature(arguments: dict[str, Any]) -> list[TextContent]:
    params = _build_literature_params(
        query=arguments.get("query", ""),
        sort=arguments.get("sort", "bestmatch"),
        page=int(arguments.get("page", 1)),
        size=int(arguments.get("size", DEFAULT_PAGE_SIZE)),
        doc_type=arguments.get("document_type"),
        language=arguments.get("language"),
        tag=arguments.get("tag"),
        experiment=arguments.get("experiment"),
    )
    client = get_http_client()
    response = await client.get(LITERATURE_ENDPOINT, params=params)
    response.raise_for_status()
    formatted = _format_search_response(response.json(), _format_hit)
    return [TextContent(type="text", text=json.dumps(formatted, indent=2))]


async def handle_get_literature(arguments: dict[str, Any]) -> list[TextContent]:
    pid = arguments.get("pid", "").strip()
    if not pid:
        raise ValueError("'pid' is required")
    record = await _fetch_record(pid)
    formatted = _format_hit(record)
    return [TextContent(type="text", text=json.dumps(formatted, indent=2))]


async def handle_get_item_locations(arguments: dict[str, Any]) -> list[TextContent]:
    pid = arguments.get("pid", "").strip()
    if not pid:
        raise ValueError("'pid' is required")

    record = await _fetch_record(pid)
    meta = record.get("metadata", {})

    items_data = meta.get("items", {}).get("hits", [])

    items = []
    shelves_seen: list[str | int] = []
    shelves_set: set[str] = set()

    for item in items_data:
        shelf = item.get("shelf")
        status = item.get("status")
        medium = item.get("medium")
        barcode = item.get("barcode")

        call_number = next(
            (i["value"] for i in item.get("identifiers", []) if i.get("scheme") == "CALL_NUMBER"),
            None,
        )

        iloc = item.get("internal_location") or {}
        location_name = iloc.get("location", {}).get("name")
        sublocation_name = iloc.get("name")

        map_url = _build_map_url([shelf]) if shelf is not None else None

        shelf_key = str(shelf)
        if shelf is not None and shelf_key not in shelves_set:
            shelves_set.add(shelf_key)
            shelves_seen.append(shelf)

        items.append({
            "barcode": barcode,
            "shelf": shelf,
            "call_number": call_number,
            "status": status,
            "medium": medium,
            "location": location_name,
            "sublocation": sublocation_name,
            "map_url": map_url,
        })

    combined_map_url = _build_map_url(shelves_seen) if shelves_seen else None

    result = {
        "pid": meta.get("pid"),
        "title": meta.get("title"),
        "total_items": meta.get("items", {}).get("total", 0),
        "available_copies": meta.get("circulation", {}).get("available_items_for_loan_count"),
        "items": items,
        "unique_shelves": list(shelves_seen),
        "combined_map_url": combined_map_url,
        "catalogue_url": f"https://catalogue.library.cern/literature/{meta.get('pid')}",
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_search_series(arguments: dict[str, Any]) -> list[TextContent]:
    q = arguments.get("query", "")
    moi = arguments.get("mode_of_issuance")
    if moi:
        q = _append_filter(q, f"mode_of_issuance:{moi.upper()}")
    params: dict[str, Any] = {
        "q": q,
        "sort": arguments.get("sort", "bestmatch"),
        "page": int(arguments.get("page", 1)),
        "size": min(int(arguments.get("size", DEFAULT_PAGE_SIZE)), MAX_PAGE_SIZE),
    }
    client = get_http_client()
    response = await client.get(SERIES_ENDPOINT, params=params)
    response.raise_for_status()
    formatted = _format_search_response(response.json(), _format_series_hit)
    return [TextContent(type="text", text=json.dumps(formatted, indent=2))]


async def handle_get_series(arguments: dict[str, Any]) -> list[TextContent]:
    pid = arguments.get("pid", "").strip()
    if not pid:
        raise ValueError("'pid' is required")
    client = get_http_client()
    response = await client.get(f"{BASE_URL}/series/{pid}")
    response.raise_for_status()
    formatted = _format_series_hit(response.json())
    return [TextContent(type="text", text=json.dumps(formatted, indent=2))]


async def handle_browse_literature(arguments: dict[str, Any]) -> list[TextContent]:
    sort_key = arguments.get("sort", "most_loaned")
    sort_param = BROWSE_SORT_MAP.get(sort_key, "-mostloaned")
    doc_type = arguments.get("document_type", "BOOK")
    size = min(int(arguments.get("size", DEFAULT_PAGE_SIZE)), MAX_PAGE_SIZE)
    page = int(arguments.get("page", 1))

    q = f'document_type:"{doc_type.upper()}"'

    client = get_http_client()
    response = await client.get(
        DOCUMENTS_ENDPOINT,
        params={"q": q, "sort": sort_param, "size": size, "page": page},
    )
    response.raise_for_status()
    formatted = _format_search_response(response.json(), _format_hit)
    return [TextContent(type="text", text=json.dumps(formatted, indent=2))]


async def handle_lookup_by_identifier(arguments: dict[str, Any]) -> list[TextContent]:
    value = arguments.get("value", "").strip()
    if not value:
        raise ValueError("'value' is required")
    # Search by identifier value; works for ISBN, DOI, report numbers
    params = {"q": f"identifiers.value:{value}", "size": 5}
    client = get_http_client()
    response = await client.get(LITERATURE_ENDPOINT, params=params)
    response.raise_for_status()
    formatted = _format_search_response(response.json(), _format_hit)
    return [TextContent(type="text", text=json.dumps(formatted, indent=2))]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="search_literature",
        description=(
            "Search the CERN Library Catalogue for books, e-books, proceedings, standards, "
            "and journals. Supports full-text queries, filtering by document type, language, "
            "CERN-specific tags (e.g. THESIS, YELLOW_REPORT), and CERN experiment (ATLAS, CMS…). "
            "Returns titles, authors, availability, and online access URLs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Full-text search query. Supports Elasticsearch syntax, "
                        "e.g. 'quantum field theory', 'title:python AND authors:knuth'."
                    ),
                },
                "sort": {
                    "type": "string",
                    "enum": ["bestmatch", "mostrecent"],
                    "default": "bestmatch",
                    "description": "Sort order for results.",
                },
                "page": {"type": "integer", "default": 1, "minimum": 1},
                "size": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Number of results per page (max 50).",
                },
                "document_type": {
                    "type": "string",
                    "enum": ["BOOK", "SERIAL", "PROCEEDINGS", "STANDARD"],
                    "description": "Filter by document type.",
                },
                "language": {
                    "type": "string",
                    "description": "Filter by ISO 639-2 language code, e.g. 'eng', 'fre', 'ger'.",
                },
                "tag": {
                    "type": "string",
                    "enum": VALID_TAGS,
                    "description": (
                        "Filter by a CERN library tag. "
                        "THESIS: doctoral theses deposited at CERN. "
                        "YELLOW_REPORT: CERN Yellow Reports (official CERN publications). "
                        "BOOKSHOP: items available from the CERN bookshop. "
                        "DIDACTIC_LIBRARY: educational materials."
                    ),
                },
                "experiment": {
                    "type": "string",
                    "description": (
                        "Filter by CERN experiment name, e.g. 'ATLAS', 'CMS', 'ALICE', 'LHCb'. "
                        "Returns literature related to or produced by that experiment."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_literature",
        description=(
            "Fetch full details for a single CERN Library record by its persistent identifier (PID). "
            "Returns extended metadata including subjects, table of contents, conference info, "
            "CERN experiment associations, and related series."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pid": {
                    "type": "string",
                    "description": "Record PID, e.g. '6hq54-tc982'. Obtained from search_literature.",
                }
            },
            "required": ["pid"],
        },
    ),
    Tool(
        name="search_series",
        description=(
            "Search for journal series and periodicals in the CERN Library Catalogue. "
            "Useful for finding recurring publications, lecture note series, or multi-volume works. "
            "Returns series title, type, ISSN, and mode of issuance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Full-text search query for the series title or ISSN.",
                },
                "mode_of_issuance": {
                    "type": "string",
                    "enum": ["SERIAL", "MULTIPART_MONOGRAPH"],
                    "description": (
                        "Filter by mode of issuance. "
                        "SERIAL: ongoing journals and periodicals. "
                        "MULTIPART_MONOGRAPH: multi-volume book sets."
                    ),
                },
                "sort": {
                    "type": "string",
                    "enum": ["bestmatch", "mostrecent"],
                    "default": "bestmatch",
                },
                "page": {"type": "integer", "default": 1, "minimum": 1},
                "size": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_series",
        description="Fetch full details for a single series record by its PID.",
        inputSchema={
            "type": "object",
            "properties": {
                "pid": {
                    "type": "string",
                    "description": "Series PID, e.g. '82j84-9e840'. Obtained from search_series.",
                }
            },
            "required": ["pid"],
        },
    ),
    Tool(
        name="browse_literature",
        description=(
            "Browse the CERN Library catalogue by newest arrivals, most-loaned titles, "
            "or most recently published. Great for discovery — finding what's popular at CERN "
            "or what just arrived in the library."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sort": {
                    "type": "string",
                    "enum": ["most_loaned", "newest", "recently_published"],
                    "default": "most_loaned",
                    "description": (
                        "most_loaned: ranked by total historical loan count (most popular). "
                        "newest: sorted by date the record was added to the catalogue. "
                        "recently_published: sorted by publication year."
                    ),
                },
                "document_type": {
                    "type": "string",
                    "enum": ["BOOK", "PROCEEDINGS", "STANDARD", "SERIAL"],
                    "default": "BOOK",
                    "description": "Type of document to browse. Defaults to BOOK.",
                },
                "size": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Number of results to return.",
                },
                "page": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 1,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="get_item_locations",
        description=(
            "Given a literature record PID, returns the physical copy locations of that book "
            "in the CERN Library, including shelf numbers, call numbers, availability status, "
            "and direct links to maps.web.cern.ch to navigate to each shelf on the CERN campus map."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pid": {
                    "type": "string",
                    "description": "Record PID, e.g. 'rqzzh-9k533'. Obtained from search_literature.",
                }
            },
            "required": ["pid"],
        },
    ),
    Tool(
        name="lookup_by_identifier",
        description=(
            "Look up a library record by a known identifier: ISBN, DOI, or report number "
            "(e.g. 'CERN-THESIS-2015-042'). Returns matching literature records."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": (
                        "The identifier value to look up. Examples: "
                        "'9783030803948' (ISBN-13), "
                        "'10.1007/978-3-030-80394-8' (DOI), "
                        "'CERN-THESIS-2020-001' (report number)."
                    ),
                }
            },
            "required": ["value"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------

app = Server("cern-library-catalogue")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    match name:
        case "search_literature":
            return await handle_search_literature(arguments)
        case "get_literature":
            return await handle_get_literature(arguments)
        case "search_series":
            return await handle_search_series(arguments)
        case "get_series":
            return await handle_get_series(arguments)
        case "lookup_by_identifier":
            return await handle_lookup_by_identifier(arguments)
        case "browse_literature":
            return await handle_browse_literature(arguments)
        case "get_item_locations":
            return await handle_get_item_locations(arguments)
        case _:
            raise ValueError(f"Unknown tool: {name!r}")


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
