#!/usr/bin/env python3
"""
Anna's Archive MCP Server — search, info, and download for ebooks and articles.

Tools:
  - searchBook: Search for books with filtering
  - searchJournal: Search for journal articles with filtering
  - info: Get detailed metadata for a record by MD5
  - fastDownload: Get fast download URL (member only)
  - downloadBook: Download a book file (member only)
  - downloadJournal: Download a journal article (member only)
  - memberDownload: Member download with quota tracking and fallback options
  - lookupDoi: Resolve a DOI to metadata and download options

Requires ANNAS_SECRET_KEY env var for download tools.
Requires ANNAS_DOWNLOAD_PATH env var for file save location (default: ~/Downloads/annas-archive).
"""

import json
import os
import re
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "annas-archive",
    instructions="Anna's Archive — ebook and article search, metadata, and download. Use searchBook/searchJournal to find content, info for metadata, and downloadBook/downloadJournal/memberDownload to retrieve files.",
)

MIRROR_DOMAINS = [
    "annas-archive.gl",
    "annas-archive.org",
    "annas-archive.li",
    "annas-archive.se",
    "annas-archive.pk",
    "annas-archive.gd",
]

_working_domain: str | None = None
_download_count: int = 0

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _get_base_url() -> str:
    global _working_domain
    if _working_domain:
        return f"https://{_working_domain}"
    for domain in MIRROR_DOMAINS:
        try:
            req = urllib.request.Request(f"https://{domain}/", headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    _working_domain = domain
                    return f"https://{domain}"
        except Exception:
            continue
    _working_domain = MIRROR_DOMAINS[0]
    return f"https://{_working_domain}"


def _get_secret_key() -> str | None:
    return os.environ.get("ANNAS_SECRET_KEY")


def _get_download_path() -> Path:
    p = Path(os.environ.get("ANNAS_DOWNLOAD_PATH", os.path.expanduser("~/Downloads/annas-archive")))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


def _parse_search_results(html: str, limit: int = 10) -> list[dict]:
    results = []
    md5s = list(dict.fromkeys(re.findall(r'/md5/([a-f0-9]{32})', html)))

    for md5 in md5s[:limit]:
        idx = html.find(f'href="/md5/{md5}"')
        if idx == -1:
            continue

        start = max(0, html.rfind('<div class="flex', 0, idx))
        end = html.find('<div class="flex', idx + 100)
        if end == -1:
            end = idx + 3000
        chunk = html[start:end]

        title_match = re.search(r'js-vim-focus[^>]*>([^<]+)</a>', chunk)
        author_match = re.search(r'mdi--user-edit[^>]*></span>\s*([^<]+)</a>', chunk)
        publisher_match = re.search(r'mdi--company[^>]*></span>\s*([^<]+)</a>', chunk)
        filepath_match = re.search(r'text-gray-500 font-mono">([^<]+)</div>', chunk)

        title = title_match.group(1).strip() if title_match else None
        author = author_match.group(1).strip() if author_match else None
        pub_info = publisher_match.group(1).strip() if publisher_match else None
        filepath = filepath_match.group(1).strip() if filepath_match else None

        year = None
        if pub_info:
            ym = re.search(r'\b(19|20)\d{2}\b', pub_info)
            if ym:
                year = ym.group(0)

        extension = None
        if filepath:
            em = re.search(r'\.(\w+)$', filepath)
            if em:
                extension = em.group(1).lower()

        if title:
            results.append({
                "md5": md5,
                "title": title,
                "author": author,
                "year": year,
                "publisher": pub_info,
                "format": extension,
            })

    return results


def _build_search_url(
    query: str,
    content_type: str | None = None,
    ext: str | None = None,
    lang: str | None = None,
    sort: str | None = None,
) -> str:
    params: dict[str, str] = {"q": query}
    if ext:
        params["ext"] = ext
    if lang:
        params["lang"] = lang
    if sort and sort != "mostRelevant":
        sort_map = {
            "newest": "year_desc",
            "oldest": "year_asc",
            "largest": "filesize_desc",
            "smallest": "filesize_asc",
        }
        params["sort"] = sort_map.get(sort, sort)
    if content_type:
        ct_map = {
            "fiction": "book_fiction",
            "nonfiction": "book_nonfiction",
            "magazine": "magazine",
            "comic": "comics",
            "musicalscore": "musical_score",
        }
        params["content"] = ct_map.get(content_type, content_type)

    base = _get_base_url()
    return f"{base}/search?{urllib.parse.urlencode(params)}"


@mcp.tool()
def searchBook(
    q: str,
    author: str | None = None,
    ext: str | None = None,
    lang: str = "en",
    cat: str | None = None,
    sort: str = "mostRelevant",
    page: int = 1,
    limit: int = 10,
) -> str:
    """Search Anna's Archive for books.

    Args:
        q: Search term — title, author, ISBN, or keywords
        author: Filter by author name (appended to query)
        ext: File type filter (epub, pdf, mobi, azw3)
        lang: Language code (default: en)
        cat: Category filter (fiction, nonfiction, magazine, comic, musicalscore)
        sort: Sort order (mostRelevant, newest, oldest, largest, smallest)
        page: Page number (default: 1)
        limit: Max results to return (default: 10, max: 20)
    """
    search_query = f"{q} {author}" if author else q
    url = _build_search_url(search_query, content_type=cat, ext=ext, lang=lang, sort=sort)
    if page > 1:
        url += f"&page={page}"

    html = _fetch(url)
    if not html:
        return json.dumps({"error": "UPSTREAM_UNAVAILABLE", "message": f"Could not reach {_working_domain or 'any mirror'}"})

    results = _parse_search_results(html, limit=min(limit, 20))
    return json.dumps({"results": results, "count": len(results), "query": search_query, "mirror": _working_domain})


@mcp.tool()
def searchJournal(
    q: str,
    author: str | None = None,
    ext: str | None = None,
    lang: str = "en",
    sort: str = "mostRelevant",
    page: int = 1,
    limit: int = 10,
) -> str:
    """Search Anna's Archive for journal articles and academic papers.

    Args:
        q: Search term — title, author, DOI, or keywords
        author: Filter by author name (appended to query)
        ext: File type filter (pdf, epub)
        lang: Language code (default: en)
        sort: Sort order (mostRelevant, newest, oldest, largest, smallest)
        page: Page number (default: 1)
        limit: Max results to return (default: 10, max: 20)
    """
    search_query = f"{q} {author}" if author else q
    url = _build_search_url(search_query, content_type="journal_article", ext=ext, lang=lang, sort=sort)
    if page > 1:
        url += f"&page={page}"

    html = _fetch(url)
    if not html:
        return json.dumps({"error": "UPSTREAM_UNAVAILABLE", "message": "Could not reach any mirror"})

    results = _parse_search_results(html, limit=min(limit, 20))
    return json.dumps({"results": results, "count": len(results), "query": search_query, "mirror": _working_domain})


def _parse_md5_page(md5: str, html: str) -> dict:
    """Extract rich metadata from an /md5/{hash} HTML page."""
    result: dict = {"md5": md5, "source": "html_fallback"}

    title_m = re.search(r'text-violet-900[^>]*data-content="([^"]+)"', html)
    if not title_m:
        title_m = re.search(r'<title>([^<]+)', html)
        result["title"] = title_m.group(1).replace(" - Anna’s Archive", "").replace(" - Anna's Archive", "").strip() if title_m else "Unknown"
    else:
        result["title"] = title_m.group(1).strip()

    author_m = re.search(r'text-amber-900[^>]*data-content="([^"]+)"', html)
    if author_m:
        result["author"] = author_m.group(1).strip()

    pub_m = re.search(r'icon-\[mdi--company\][^>]*></span>\s*([^<]+)</a>', html)
    if pub_m:
        pub_text = pub_m.group(1).strip()
        result["publisher"] = pub_text
        year_m = re.search(r'\b(19|20)\d{2}\b', pub_text)
        if year_m:
            result["year"] = year_m.group(0)

    desc_m = re.search(r'js-md5-top-box-description[^>]*>(?:<div[^>]*>[^<]*</div>)?<div[^>]*>([^<]+)', html)
    if desc_m:
        result["description"] = desc_m.group(1).strip()[:500]

    cover_m = re.search(r'<img[^>]*object-cover[^>]*src="([^"]+)"', html)
    if cover_m:
        result["cover_url"] = cover_m.group(1)

    isbn13s = list(dict.fromkeys(re.findall(r'isbn13:(\d{13})', html)))
    isbn10s = list(dict.fromkeys(re.findall(r'isbn10:(\d{10})', html)))
    dois = list(dict.fromkeys(
        d.rstrip('“”"\'') for d in re.findall(r'doi:(10\.\d{4,}/[^"&<\s]+)', html)
    ))
    if isbn13s or isbn10s or dois:
        result["identifiers"] = {}
        if isbn13s:
            result["identifiers"]["isbn13"] = isbn13s
        if isbn10s:
            result["identifiers"]["isbn10"] = isbn10s
        if dois:
            result["identifiers"]["doi"] = dois

    filesize_m = re.search(r'filesize_bytes:(\d+)', html)
    if filesize_m:
        result["filesize_bytes"] = int(filesize_m.group(1))

    lang_codes = list(dict.fromkeys(re.findall(r'"lang:([a-z]{2})"', html)))
    if lang_codes:
        result["language_codes"] = lang_codes

    filepath_m = re.search(r'Filepath</span><span[^>]*>([^<]+)', html)
    if filepath_m:
        ext_m = re.search(r'\.(\w+)$', filepath_m.group(1).strip())
        if ext_m:
            result["extension"] = ext_m.group(1).lower()

    alt_pubs = re.findall(r'Alternative publisher</div><div[^>]*>([^<]+)', html)
    if alt_pubs:
        result["alternative_publishers"] = alt_pubs

    return result


@mcp.tool()
def info(md5: str) -> str:
    """Get detailed metadata for a book or journal by its MD5 hash.

    Returns title, author, publisher, year, identifiers (ISBN, DOI, OCLC),
    description, cover URL, download options, IPFS CIDs, and more.

    Args:
        md5: 32-character MD5 hash from a search result
    """
    base = _get_base_url()
    url = f"{base}/db/aarecord_elasticsearch/md5:{md5}.json"
    raw = _fetch(url)

    if not raw:
        url_fallback = f"{base}/md5/{md5}"
        html = _fetch(url_fallback)
        if not html:
            return json.dumps({"error": "NOT_FOUND", "md5": md5})
        return json.dumps(_parse_md5_page(md5, html))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return json.dumps({"error": "PARSE_ERROR", "md5": md5})

    fud = data.get("file_unified_data", {})
    additional = data.get("additional", {})
    identifiers = fud.get("identifiers_unified", {})

    result = {
        "md5": md5,
        "title": fud.get("title_best", ""),
        "author": fud.get("author_best", ""),
        "publisher": fud.get("publisher_best", ""),
        "year": fud.get("year_best", ""),
        "edition": fud.get("edition_varia_best", ""),
        "extension": fud.get("extension_best", ""),
        "filesize_bytes": fud.get("filesize_best", 0),
        "language_codes": fud.get("language_codes", []),
        "content_type": fud.get("content_type_best", ""),
        "description": (fud.get("stripped_description_best", "") or "")[:500],
        "cover_url": fud.get("cover_url_best", ""),
        "identifiers": {
            "isbn13": identifiers.get("isbn13", []),
            "isbn10": identifiers.get("isbn10", []),
            "doi": identifiers.get("doi", []),
            "oclc": identifiers.get("oclc", []),
            "ol": identifiers.get("ol", []),
        },
        "download_options": [
            {"source": d[0], "url": d[1], "note": d[2] if len(d) > 2 else ""}
            for d in additional.get("download_urls", [])
        ][:8],
        "ipfs_cids": [i.get("ipfs_cid", "") for i in fud.get("ipfs_infos", []) if i.get("ipfs_cid")],
        "has_fast_download": bool(additional.get("partner_url_paths")),
        "has_torrent": bool(fud.get("has_torrent_paths")),
        "filename": additional.get("filename", ""),
    }

    return json.dumps(result)


@mcp.tool()
def fastDownload(md5: str, path_index: int = 0, domain_index: int = 0) -> str:
    """Get a fast download URL for a book or journal (member only).

    Returns the direct download URL without downloading the file.
    Requires ANNAS_SECRET_KEY environment variable.

    Args:
        md5: 32-character MD5 hash from a search result
        path_index: Storage path index (default: 0 = fastest)
        domain_index: Download server index (default: 0)
    """
    key = _get_secret_key()
    if not key:
        return json.dumps({"error": "AUTH_REQUIRED", "message": "ANNAS_SECRET_KEY environment variable not set"})

    params = {"md5": md5, "key": key, "path_index": path_index, "domain_index": domain_index}
    base = _get_base_url()
    url = f"{base}/dyn/api/fast_download.json?{urllib.parse.urlencode(params)}"

    raw = _fetch(url)
    if not raw:
        return json.dumps({"error": "UPSTREAM_UNAVAILABLE", "md5": md5})

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return json.dumps({"error": "PARSE_ERROR", "md5": md5, "raw": raw[:200]})

    if data.get("error"):
        error_msg = data["error"]
        if "member" in error_msg.lower():
            return json.dumps({"error": "AUTH_INVALID", "message": error_msg})
        return json.dumps({"error": "API_ERROR", "message": error_msg})

    download_url = data.get("download_url")
    if not download_url:
        return json.dumps({"error": "NO_DOWNLOAD_URL", "md5": md5, "response": data})

    return json.dumps({"download_url": download_url, "md5": md5})


def _download_file(md5: str, output_dir: Path | None = None, path_index: int = 0, domain_index: int = 0) -> dict:
    """Internal: download a file via fast_download API with atomic write."""
    global _download_count

    key = _get_secret_key()
    if not key:
        return {"error": "AUTH_REQUIRED", "message": "ANNAS_SECRET_KEY not set"}

    params = {"md5": md5, "key": key, "path_index": path_index, "domain_index": domain_index}
    base = _get_base_url()
    api_url = f"{base}/dyn/api/fast_download.json?{urllib.parse.urlencode(params)}"

    raw = _fetch(api_url)
    if not raw:
        return {"error": "UPSTREAM_UNAVAILABLE", "md5": md5}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "PARSE_ERROR", "md5": md5}

    if data.get("error"):
        err = data["error"]
        if "quota" in err.lower() or "limit" in err.lower():
            return {"error": "QUOTA_EXHAUSTED", "message": err, "downloads_this_session": _download_count}
        if "member" in err.lower() or "key" in err.lower():
            return {"error": "AUTH_INVALID", "message": err}
        return {"error": "API_ERROR", "message": err}

    download_url = data.get("download_url")
    if not download_url:
        return {"error": "NO_DOWNLOAD_URL", "md5": md5}

    dest = output_dir or _get_download_path()
    dest.mkdir(parents=True, exist_ok=True)

    filename = urllib.parse.unquote(download_url.split("/")[-1].split("?")[0])
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
    if not filename or filename == md5:
        filename = f"{md5}.pdf"

    filepath = dest / filename
    tmp_path = dest / f".{filename}.tmp"

    if filepath.exists():
        return {
            "success": True,
            "filepath": str(filepath),
            "filename": filename,
            "skipped": True,
            "message": "File already exists",
        }

    try:
        urllib.request.urlretrieve(download_url, str(tmp_path))
        tmp_path.rename(filepath)
        _download_count += 1
        return {
            "success": True,
            "filepath": str(filepath),
            "filename": filename,
            "filesize_bytes": filepath.stat().st_size,
            "downloads_this_session": _download_count,
        }
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return {"error": "DOWNLOAD_FAILED", "message": str(e), "md5": md5}


@mcp.tool()
def downloadBook(md5: str, output_path: str | None = None, path_index: int = 0, domain_index: int = 0) -> str:
    """Download a book by MD5 hash using the fast download API.

    Saves to ANNAS_DOWNLOAD_PATH or the specified output_path.
    Uses atomic writes (temp file + rename) to prevent partial files.
    Skips if the file already exists.

    Args:
        md5: 32-character MD5 hash from a search result
        output_path: Directory to save file (default: ANNAS_DOWNLOAD_PATH)
        path_index: Storage path index (default: 0)
        domain_index: Download server index (default: 0)
    """
    dest = Path(output_path) if output_path else None
    result = _download_file(md5, output_dir=dest, path_index=path_index, domain_index=domain_index)
    return json.dumps(result)


@mcp.tool()
def downloadJournal(md5: str, output_path: str | None = None) -> str:
    """Download a journal article by MD5 hash.

    Args:
        md5: 32-character MD5 hash from a search result
        output_path: Directory to save file (default: ANNAS_DOWNLOAD_PATH)
    """
    dest = Path(output_path) if output_path else None
    result = _download_file(md5, output_dir=dest)
    return json.dumps(result)


@mcp.tool()
def memberDownload(md5: str, output_path: str | None = None) -> str:
    """Member-only download with quota tracking and account info.

    Returns the download result plus current session quota usage.
    On quota exhaustion, returns IPFS and alternative download links as fallback.

    Args:
        md5: 32-character MD5 hash from a search result
        output_path: Directory to save file (default: ANNAS_DOWNLOAD_PATH)
    """
    key = _get_secret_key()
    if not key:
        return json.dumps({"error": "AUTH_REQUIRED", "message": "ANNAS_SECRET_KEY not set"})

    dest = Path(output_path) if output_path else None
    result = _download_file(md5, output_dir=dest)

    result["member_key_prefix"] = key[:8] + "..."
    result["downloads_this_session"] = _download_count
    result["quota_note"] = "Quota resets on a rolling 18-hour window"

    if result.get("error") == "QUOTA_EXHAUSTED":
        details_raw = info(md5)
        details = json.loads(details_raw)
        result["fallback_options"] = {
            "ipfs_cids": details.get("ipfs_cids", []),
            "download_options": details.get("download_options", [])[:4],
        }

    return json.dumps(result)


@mcp.tool()
def lookupDoi(doi: str) -> str:
    """Resolve a DOI to Anna's Archive metadata and download options.

    Args:
        doi: Digital Object Identifier (e.g., 10.1038/nature12373)
    """
    base = _get_base_url()
    url = f"{base}/scidb/{urllib.parse.quote(doi, safe='')}"
    html = _fetch(url)

    if not html:
        return json.dumps({"error": "UPSTREAM_UNAVAILABLE", "doi": doi})

    md5s = list(dict.fromkeys(re.findall(r'/md5/([a-f0-9]{32})', html)))
    if not md5s:
        return json.dumps({"error": "NOT_FOUND", "doi": doi, "message": "No files found for this DOI"})

    results = []
    for md5 in md5s[:3]:
        detail = json.loads(info(md5))
        if not detail.get("error"):
            results.append(detail)

    return json.dumps({"doi": doi, "results": results, "count": len(results)})


# ---------------------------------------------------------------------------
# Text extraction, search, and RAG integration
# ---------------------------------------------------------------------------

def _extract_epub_text(filepath: Path) -> str:
    """Extract plain text from an EPUB file using ebooklib + BeautifulSoup."""
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(str(filepath), options={"ignore_ncx": True})
    texts = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "lxml")
        text = soup.get_text(separator="\n", strip=True)
        if text:
            texts.append(text)
    return "\n\n".join(texts)


def _extract_pdf_text(filepath: Path) -> str:
    """Extract plain text from a PDF file using PyMuPDF."""
    import fitz

    doc = fitz.open(str(filepath))
    texts = []
    for page in doc:
        text = page.get_text()
        if text.strip():
            texts.append(text)
    doc.close()
    return "\n\n".join(texts)


def _extract_text(filepath: Path) -> tuple[str, str]:
    """Extract text from a file. Returns (text, format)."""
    suffix = filepath.suffix.lower()
    if suffix == ".epub":
        return _extract_epub_text(filepath), "epub"
    elif suffix == ".pdf":
        return _extract_pdf_text(filepath), "pdf"
    elif suffix in (".txt", ".md", ".rst"):
        return filepath.read_text(encoding="utf-8", errors="replace"), suffix.lstrip(".")
    else:
        raise ValueError(f"Unsupported format: {suffix}. Supported: epub, pdf, txt, md")


@mcp.tool()
def extractText(
    filepath: str,
    max_chars: int = 0,
    output_path: str | None = None,
) -> str:
    """Extract plain text from an ebook file (EPUB, PDF, TXT).

    Returns extracted text content. For large files, use max_chars to truncate
    or output_path to save to a file instead of returning inline.

    Args:
        filepath: Path to the ebook file (epub, pdf, txt, md)
        max_chars: Max characters to return (0 = unlimited). Use for previews
        output_path: Save extracted text to this file instead of returning inline
    """
    p = Path(filepath)
    if not p.exists():
        return json.dumps({"error": "FILE_NOT_FOUND", "filepath": filepath})

    try:
        text, fmt = _extract_text(p)
    except ValueError as e:
        return json.dumps({"error": "UNSUPPORTED_FORMAT", "message": str(e)})
    except Exception as e:
        return json.dumps({"error": "EXTRACTION_FAILED", "message": str(e), "filepath": filepath})

    char_count = len(text)
    word_count = len(text.split())

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return json.dumps({
            "success": True,
            "output_path": str(out),
            "source_format": fmt,
            "char_count": char_count,
            "word_count": word_count,
        })

    if max_chars and char_count > max_chars:
        text = text[:max_chars]

    return json.dumps({
        "text": text,
        "source_format": fmt,
        "char_count": char_count,
        "word_count": word_count,
        "truncated": bool(max_chars and char_count > max_chars),
    })


@mcp.tool()
def searchText(
    filepath: str,
    query: str,
    context_chars: int = 200,
    max_results: int = 10,
) -> str:
    """Search within an ebook file for text matching a query.

    Extracts text from the file and finds all occurrences of the query,
    returning surrounding context for each match.

    Args:
        filepath: Path to the ebook file (epub, pdf, txt, md)
        query: Text to search for (case-insensitive)
        context_chars: Characters of context to show around each match (default: 200)
        max_results: Maximum matches to return (default: 10)
    """
    p = Path(filepath)
    if not p.exists():
        return json.dumps({"error": "FILE_NOT_FOUND", "filepath": filepath})

    try:
        text, fmt = _extract_text(p)
    except Exception as e:
        return json.dumps({"error": "EXTRACTION_FAILED", "message": str(e)})

    lower_text = text.lower()
    lower_query = query.lower()
    matches = []
    start = 0

    while len(matches) < max_results:
        idx = lower_text.find(lower_query, start)
        if idx == -1:
            break
        ctx_start = max(0, idx - context_chars)
        ctx_end = min(len(text), idx + len(query) + context_chars)
        matches.append({
            "position": idx,
            "context": text[ctx_start:ctx_end],
        })
        start = idx + len(query)

    return json.dumps({
        "query": query,
        "source": filepath,
        "source_format": fmt,
        "total_matches": len(matches),
        "matches": matches,
    })


@mcp.tool()
def chunkForRAG(
    filepath: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> str:
    """Extract text from an ebook and split into chunks suitable for RAG ingestion.

    Returns chunks with metadata (source file, chunk index, total chunks).
    Each chunk is sized for embedding models (default 1000 chars with 200 overlap).
    Output can be piped to a vector DB ingestion endpoint.

    Args:
        filepath: Path to the ebook file (epub, pdf, txt)
        chunk_size: Characters per chunk (default: 1000, matches jarvis-rag)
        chunk_overlap: Overlap between chunks (default: 200)
    """
    p = Path(filepath)
    if not p.exists():
        return json.dumps({"error": "FILE_NOT_FOUND", "filepath": filepath})

    try:
        text, fmt = _extract_text(p)
    except Exception as e:
        return json.dumps({"error": "EXTRACTION_FAILED", "message": str(e)})

    chunks = []
    pos = 0
    while pos < len(text):
        end = pos + chunk_size
        chunk_text = text[pos:end]

        # Try to break at sentence boundary
        if end < len(text):
            for sep in [". ", ".\n", "! ", "? ", "\n\n"]:
                last_sep = chunk_text.rfind(sep)
                if last_sep > chunk_size * 0.5:
                    chunk_text = chunk_text[:last_sep + len(sep)]
                    end = pos + len(chunk_text)
                    break

        if chunk_text.strip():
            chunks.append({
                "text": chunk_text.strip(),
                "chunk_index": len(chunks),
                "char_offset": pos,
            })

        pos = end - chunk_overlap if end < len(text) else end

    return json.dumps({
        "source": filepath,
        "source_format": fmt,
        "total_chunks": len(chunks),
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "total_chars": len(text),
        "total_words": len(text.split()),
        "chunks": chunks,
    })


if __name__ == "__main__":
    mcp.run(transport="stdio")
