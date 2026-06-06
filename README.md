<p align="center">
  <img src="https://img.shields.io/badge/MCP-FastMCP_3.x-blueviolet?style=for-the-badge" alt="FastMCP 3.x"/>
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/Anna's_Archive-169M+_files-orange?style=for-the-badge" alt="Anna's Archive"/>
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="MIT"/>
</p>

# AnnasTools

**An MCP server for programmatic book and article retrieval from Anna's Archive** — the world's largest open-source shadow library aggregator (169M+ indexed files across Libgen, Z-Library, Sci-Hub, Internet Archive, and direct uploads).

AnnasTools exposes 8 tools over the [Model Context Protocol](https://modelcontextprotocol.io/), enabling any MCP-compatible AI assistant (Claude Code, Claude Desktop, Cursor, etc.) to search for books and academic papers, retrieve detailed metadata, and download files using a paid member account.

---

## Why This Exists

Anna's Archive has **no REST API**. The project explicitly states that their data endpoints are "not intended as an API." The entire search surface is server-rendered HTML, and the only official JSON endpoint — `fast_download.json` — is gated behind paid membership.

This creates a gap: AI assistants that can call MCP tools have no programmatic way to search or retrieve content from what is effectively the largest bibliographic index on the internet.

AnnasTools bridges that gap with a pure-Python MCP server that:
- **Scrapes search results** from the HTML frontend with regex-based extraction
- **Parses metadata pages** into structured JSON with 12+ fields (title, author, publisher, ISBNs, DOIs, description, cover URL, filesize, language, format)
- **Authenticates via member key** for the `fast_download.json` API — the only reliable programmatic download path
- **Handles mirror failover** across 6 domain mirrors automatically
- **Writes files atomically** (temp file → rename) to prevent partial downloads on network interruption

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│  AI Assistant (Claude Code, Claude Desktop, Cursor, etc.)     │
│                          │                                     │
│                   MCP stdio transport                          │
│                          │                                     │
├──────────────────────────┼────────────────────────────────────┤
│               AnnasTools MCP Server                            │
│                          │                                     │
│  ┌─────────────┐  ┌─────┴──────┐  ┌────────────────────────┐ │
│  │  Search      │  │  Metadata  │  │  Download              │ │
│  │              │  │            │  │                        │ │
│  │ searchBook   │  │  info      │  │ fastDownload           │ │
│  │ searchJournal│  │  lookupDoi │  │ downloadBook           │ │
│  │              │  │            │  │ downloadJournal        │ │
│  │              │  │            │  │ memberDownload         │ │
│  └──────┬──────┘  └─────┬──────┘  └──────────┬─────────────┘ │
│         │               │                     │               │
│    HTML scrape     ES JSON → HTML         fast_download.json  │
│    (regex)         fallback chain          (member key auth)  │
│         │               │                     │               │
├─────────┴───────────────┴─────────────────────┴───────────────┤
│                   Anna's Archive (6 mirrors)                   │
│                                                                │
│  annas-archive.gl  .org  .li  .se  .pk  .gd                  │
│                                                                │
│  Search: /search?q=...  (HTML, scraped)                       │
│  Detail: /md5/{hash}    (HTML, parsed for 12+ fields)         │
│  Record: /db/aarecord_elasticsearch/md5:{hash}.json  (403s)   │
│  Download: /dyn/api/fast_download.json  (member key required) │
│  DOI: /scidb/{doi}      (HTML, scraped for MD5 links)         │
└───────────────────────────────────────────────────────────────┘
```

### Data Flow

**Search path** (no authentication required):
1. Build query URL with filters (format, language, category, sort)
2. Fetch HTML from `/search?q=...` on the first reachable mirror
3. Extract MD5 hashes via regex: `/md5/([a-f0-9]{32})`
4. Parse surrounding HTML divs for title, author, publisher, year, format
5. Return structured JSON array

**Metadata path** (no authentication required):
1. Try ElasticSearch JSON endpoint: `/db/aarecord_elasticsearch/md5:{hash}.json`
2. If 403 (common — Cloudflare blocks many requests), fall back to HTML page: `/md5/{hash}`
3. Parse HTML with targeted regex selectors for `data-content` attributes, Iconify class names, and inline search-link patterns
4. Extract: title, author, publisher, year, description, cover URL, ISBN-13/10, DOI, filesize, language codes, file extension, alternative publishers

**Download path** (member key required):
1. Call `fast_download.json` with MD5 + member secret key
2. Receive direct download URL
3. Download to temp file (`.filename.tmp`)
4. Atomic rename to final path on success
5. Clean up temp file on failure
6. Skip if file already exists at target path

---

## Tool Reference

### Search Tools

| Tool | Auth | Description |
|------|------|-------------|
| `searchBook` | No | Search for books with title, author, format, language, category, and sort filters |
| `searchJournal` | No | Search for journal articles and academic papers with the same filter set |

**Parameters** (both tools):

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | required | Search query — title, author, ISBN, or keywords |
| `author` | string | null | Author name filter (appended to query) |
| `ext` | string | null | File format: `epub`, `pdf`, `mobi`, `azw3` |
| `lang` | string | `en` | Language code |
| `cat` | string | null | Category: `fiction`, `nonfiction`, `magazine`, `comic`, `musicalscore` |
| `sort` | string | `mostRelevant` | Sort: `mostRelevant`, `newest`, `oldest`, `largest`, `smallest` |
| `page` | int | 1 | Results page number |
| `limit` | int | 10 | Max results (capped at 20) |

**Response format:**
```json
{
  "results": [
    {
      "md5": "ba3e5c873982e0a623b27333916f6e2a",
      "title": "Clean Code: A Handbook of Agile Software Craftsmanship ; Second Edition",
      "author": "Robert C. Martin",
      "year": "2025",
      "publisher": "Pearson, 2., 2025",
      "format": "epub"
    }
  ],
  "count": 3,
  "query": "Clean Code Robert Martin",
  "mirror": "annas-archive.gl"
}
```

### Metadata Tools

| Tool | Auth | Description |
|------|------|-------------|
| `info` | No | Get detailed metadata for any record by MD5 hash |
| `lookupDoi` | No | Resolve a DOI to Anna's Archive records with full metadata |

**`info` response** (HTML fallback path — covers ~90% of requests):
```json
{
  "md5": "ba3e5c873982e0a623b27333916f6e2a",
  "source": "html_fallback",
  "title": "Clean Code: A Handbook of Agile Software Craftsmanship ; Second Edition",
  "author": "Robert C. Martin",
  "publisher": "Pearson, 2., 2025",
  "year": "2025",
  "description": "This new edition of the classic guide to software craftsmanship...",
  "cover_url": "https://covers.z-lib.sk/covers400/collections/userbooks/dc8aa1ff...",
  "identifiers": {
    "isbn13": ["9780135398586"],
    "isbn10": ["0135398584"]
  },
  "filesize_bytes": 17605338,
  "language_codes": ["en"],
  "extension": "epub",
  "alternative_publishers": ["Globe Fearon Educational Publishing", "Longman Publishing"]
}
```

When the ElasticSearch JSON endpoint succeeds (rarer), the response includes additional fields: `edition`, `content_type`, `download_options`, `ipfs_cids`, `has_fast_download`, `has_torrent`, and `filename`.

### Download Tools

| Tool | Auth | Description |
|------|------|-------------|
| `fastDownload` | Yes | Get a direct download URL without saving (URL only) |
| `downloadBook` | Yes | Download a book to disk with atomic write and skip-existing |
| `downloadJournal` | Yes | Download a journal article to disk |
| `memberDownload` | Yes | Download with quota tracking; provides IPFS fallback on quota exhaustion |

**`downloadBook` response:**
```json
{
  "success": true,
  "filepath": "/Users/you/Downloads/annas-archive/Clean Code... .epub",
  "filename": "Clean Code... .epub",
  "filesize_bytes": 17605338,
  "downloads_this_session": 1
}
```

**Error codes** (all download tools):

| Code | Meaning |
|------|---------|
| `AUTH_REQUIRED` | `ANNAS_SECRET_KEY` env var not set |
| `AUTH_INVALID` | Member key rejected by Anna's Archive |
| `QUOTA_EXHAUSTED` | Download quota exceeded (resets on 18-hour rolling window) |
| `UPSTREAM_UNAVAILABLE` | All mirror domains unreachable |
| `NO_DOWNLOAD_URL` | API returned success but no download URL |
| `DOWNLOAD_FAILED` | Network error during file transfer |
| `PARSE_ERROR` | JSON response was not parseable |
| `NOT_FOUND` | MD5 hash not found in any mirror |

---

## Installation

### Prerequisites

- Python 3.11+
- An Anna's Archive member account (for download tools — search and metadata work without one)

### Setup

```bash
git clone https://github.com/CannonCoPilot/AnnasTools.git
cd AnnasTools

python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Claude Code Integration

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "annas-archive": {
      "command": "/path/to/AnnasTools/.venv/bin/python",
      "args": ["/path/to/AnnasTools/src/annas_tools/mcp_server.py"],
      "env": {
        "ANNAS_SECRET_KEY": "your-member-secret-key",
        "ANNAS_DOWNLOAD_PATH": "~/Downloads/annas-archive"
      }
    }
  }
}
```

Restart Claude Code to load the server. Verify with: `ToolSearch("select:mcp__annas-archive__searchBook")`

### Claude Desktop Integration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "annas-archive": {
      "command": "/path/to/AnnasTools/.venv/bin/python",
      "args": ["/path/to/AnnasTools/src/annas_tools/mcp_server.py"],
      "env": {
        "ANNAS_SECRET_KEY": "your-member-secret-key",
        "ANNAS_DOWNLOAD_PATH": "~/Downloads/annas-archive"
      }
    }
  }
}
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANNAS_SECRET_KEY` | For downloads | (none) | Member account secret key from Anna's Archive settings |
| `ANNAS_DOWNLOAD_PATH` | No | `~/Downloads/annas-archive` | Directory for downloaded files |

---

## Implementation Details

### Why HTML Scraping Instead of API

Anna's Archive is designed as a web-first application. Their ElasticSearch data is accessible at `/db/aarecord_elasticsearch/md5:{hash}.json`, but the project explicitly labels this as "not an API" and recommends mirroring their 1.5TB data dump for programmatic access. In practice:

- **Search** has no API equivalent — HTML scraping is the only path
- **ElasticSearch JSON** returns HTTP 403 for a significant fraction of MD5 hashes (Cloudflare-mediated), making it unreliable as a primary metadata source
- **`fast_download.json`** is the sole official programmatic endpoint, and it only handles downloads

Every existing implementation in the ecosystem (Go, Rust, Python, Dart) scrapes HTML for search. AnnasTools follows this established pattern.

### HTML Parser Design

**Search results** (`_parse_search_results`):
- Extracts MD5 hashes via `/md5/([a-f0-9]{32})` regex
- For each MD5, isolates the surrounding `<div class="flex">` block
- Parses with CSS-class-anchored regex: `js-vim-focus` (title), `mdi--user-edit` (author), `mdi--company` (publisher), `text-gray-500 font-mono` (filepath/format)

**Metadata pages** (`_parse_md5_page`):
- Uses `data-content` attributes on color-coded divs: `text-violet-900` (title), `text-amber-900` (author)
- Extracts identifiers from inline search-link patterns: `isbn13:(\d{13})`, `isbn10:(\d{10})`, `doi:(10\.\d{4,}/...)`
- Reads filesize from `filesize_bytes:(\d+)` search links
- Parses description from `js-md5-top-box-description` container
- Extracts cover URL from `<img>` with `object-cover` class
- Cleans DOI strings by stripping trailing Unicode smart quotes (`”`)

### Mirror Failover

On first request, the server probes 6 known mirror domains in order until one responds with HTTP 200. The working domain is cached for the lifetime of the process. Mirror list:

```
annas-archive.gl → .org → .li → .se → .pk → .gd
```

### Atomic File Writes

Downloads follow the write-temp-then-rename pattern:
1. Write to `.filename.tmp` in the target directory
2. On success: `rename(.filename.tmp → filename)` — atomic on POSIX filesystems
3. On failure: `unlink(.filename.tmp)` — no partial file left behind
4. On pre-existing file: skip download entirely, return `skipped: true`

This prevents corrupt files from appearing in the download directory during network interruptions.

### Session-Scoped Quota Tracking

The server maintains a process-level `_download_count` counter. The `memberDownload` tool reports this in every response, and on `QUOTA_EXHAUSTED` errors, automatically fetches IPFS CIDs and alternative download links as fallback options.

Anna's Archive member download quota resets on a rolling 18-hour window. The exact quota limit is not published and varies by membership tier.

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `fastmcp` | ≥3.0.0 | MCP server framework — tool registration, stdio transport, schema generation |
| `httpx` | ≥0.27.0 | Listed as dependency but not used at runtime (stdlib `urllib` handles all HTTP) |
| `click` | ≥8.1.0 | CLI framework (reserved for future CLI interface) |
| `mcp` | 1.27.2 | Core MCP protocol implementation (transitive via fastmcp) |
| `pydantic` | 2.13.4 | Schema validation (transitive via fastmcp) |

> **Note:** The server deliberately uses `urllib.request` instead of `httpx` or `requests` to minimize the dependency tree. The `httpx` dependency in `pyproject.toml` is reserved for future async transport support.

---

## Typical Usage Patterns

### Search and Download a Book

```
User: "Find and download Clean Code by Robert Martin in EPUB format"

1. searchBook(q="Clean Code", author="Robert Martin", ext="epub")
   → Returns 3 results with MD5 hashes

2. info(md5="ba3e5c873982e0a623b27333916f6e2a")
   → Returns full metadata: 2025 2nd edition, Pearson, ISBN 9780135398586

3. downloadBook(md5="ba3e5c873982e0a623b27333916f6e2a")
   → Downloads 17 MB EPUB to ~/Downloads/annas-archive/
```

### Look Up an Academic Paper by DOI

```
User: "Get the paper at DOI 10.1038/nature12373"

1. lookupDoi(doi="10.1038/nature12373")
   → Resolves to MD5 hashes, returns metadata for up to 3 matching files

2. downloadJournal(md5="<hash>")
   → Downloads PDF to default path
```

### Batch Retrieval via Alfred Persona

When integrated with the Alfred Archon's `book-retriever` persona, requests can be submitted as Pulse tasks:

```bash
pulse create "Retrieve: Designing Data-Intensive Applications by Kleppmann" \
  -t task -p 3 \
  -l "domain:knowledge,source:headless,action:retrieve,agent:aifred" \
  -e 5
```

The persona picks up the task, searches, downloads, and closes it with results.

---

## Project Structure

```
AnnasTools/
├── src/
│   └── annas_tools/
│       └── mcp_server.py          # MCP server — 8 tools, ~570 LOC
├── tests/                          # Test directory (download artifacts)
├── docs/                           # Documentation assets
├── pyproject.toml                  # Package metadata and dependencies
├── .gitignore                      # Excludes .venv, downloads, __pycache__
└── README.md                       # This file
```

---

## Future Work

- **Hot-reload wrapper**: Wrap the server with `mcp-hot-reload` for automatic restart on code changes (currently requires manual MCP process restart)
- **Async transport**: Migrate HTTP calls from `urllib.request` to `httpx.AsyncClient` for concurrent metadata lookups within `lookupDoi`
- **Local metadata index**: Ingest Anna's Archive data dumps (torrents) into a local SQLite/PostgreSQL database for sub-millisecond search without network dependency
- **Cover image caching**: Download and cache cover images locally for offline browsing
- **Format conversion pipeline**: Post-download conversion (e.g., EPUB → MOBI via Calibre) as an additional tool
- **Search result enrichment**: Parallel `info` calls on search results to return full metadata in a single search response
- **Rate limit awareness**: Respect Cloudflare rate limits with exponential backoff on 429/503 responses
- **CLI interface**: Click-based CLI for standalone use outside MCP (search, download, batch import)

---

## License

MIT License — see [LICENSE](LICENSE).
