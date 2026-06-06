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
        title_match = re.search(r'<title>([^<]+)', html)
        return json.dumps({
            "md5": md5,
            "title": title_match.group(1).replace(" - Anna's Archive", "").strip() if title_match else "Unknown",
            "source": "html_fallback",
        })

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
