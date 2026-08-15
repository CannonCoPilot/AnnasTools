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
# Per-domain probe verdicts from the last selection sweep: {domain: cause}. Kept so a failure
# can say WHY each mirror was rejected instead of collapsing six different worlds into one
# unfalsifiable "could not reach any mirror".
_probe_report: dict[str, str] = {}

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _probe_domain(domain: str) -> tuple[bool, str]:
    """Probe a mirror and return (usable, cause).

    PROBE THE ENDPOINT YOU ACTUALLY USE. The previous version probed `/`, but `/` is not what
    this server ever requests — and the two disagree. Measured 2026-08-15:

        .gl  /=200  /search -> 302 -> ?check=1 -> 403 DDoS-Guard JS challenge
        .pk  /=200  /search -> 302 (same)
        .gd  /=200  /search -> 302 (same)
        .li  /=200  /search=200 but ZERO results (JS interstitial body)
        .org/.se  DNS dead

    So every mirror passed the old `/` probe, and the first one — `.gl` — was cached forever
    while being incapable of serving a single search.

    A 200 IS NOT A RESULT. `.li` answers 200 with an interstitial containing no /md5/ links, so
    status alone cannot decide usability; the probe requires actual parseable content. This is
    the same lesson as a boolean health check that reports "up" for a service returning errors.
    """
    url = f"https://{domain}/search?q=test"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        # urlopen FOLLOWS redirects, so a 302 to a challenge page arrives here as the challenge,
        # not as an exception — which is exactly how this failure stayed invisible.
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return False, f"http-{resp.status}"
            body = resp.read().decode("utf-8", errors="replace")
            if re.search(r"/md5/[a-f0-9]{32}", body):
                return True, "ok"
            low = body.lower()
            if "ddos-guard" in low or "js-challenge" in low or "fingerprint" in low:
                return False, "anti-bot-challenge"
            return False, "200-but-no-results"
    except urllib.error.HTTPError as e:
        return False, f"http-{e.code}"
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "Name or service not known" in reason or "nodename nor servname" in reason:
            return False, "dns-dead"
        return False, f"unreachable ({reason[:40]})"
    except Exception as e:  # noqa: BLE001 — cause is reported, never swallowed
        return False, f"error ({type(e).__name__})"


def _get_base_url() -> str | None:
    """Return a base URL for a mirror that can actually serve searches, or None.

    RETURNS None WHEN NOTHING WORKS. The previous version ended with
    `_working_domain = MIRROR_DOMAINS[0]` — recording a working mirror when every probe had
    FAILED. Absence of a usable mirror is not a usable mirror, and that fallback is what turned
    a total outage into a confident lock onto a dead domain.
    """
    global _working_domain
    if _working_domain:
        return f"https://{_working_domain}"
    _probe_report.clear()
    for domain in MIRROR_DOMAINS:
        ok, cause = _probe_domain(domain)
        _probe_report[domain] = cause
        if ok:
            _working_domain = domain
            return f"https://{domain}"
    return None


def _upstream_error(action: str = "reach a mirror") -> str:
    """A failure that names each mirror's cause, so the remedy is decidable from the message."""
    detail = ", ".join(f"{d}: {c}" for d, c in _probe_report.items()) or "no probe run"
    hint = ""
    # Judge only the REACHABLE mirrors. A first version required EVERY cause to be
    # challenge-like, so two dns-dead domains suppressed the hint in the exact situation it
    # describes — every mirror we can still reach is challenge-gated. Unreachable hosts say
    # nothing about whether access is being refused, so they must not vote.
    def _blocked(c: str) -> bool:
        return c in ("anti-bot-challenge", "200-but-no-results") or c.startswith("http-40")

    reachable = [c for c in _probe_report.values() if c != "dns-dead" and not c.startswith("unreachable")]
    if reachable and all(_blocked(c) for c in reachable):
        hint = (" Every reachable mirror is behind an anti-bot challenge requiring JS execution."
                " That is an access control, not a transport fault — retrying will not help."
                " For open-access papers use Unpaywall (api.unpaywall.org/v2/<doi>?email=...).")
    return json.dumps({
        "error": "UPSTREAM_UNAVAILABLE",
        "message": f"Could not {action}.{hint}",
        "mirror_causes": detail,
    })


def _get_secret_key() -> str | None:
    return os.environ.get("ANNAS_SECRET_KEY")


def _get_download_path() -> Path:
    p = Path(os.environ.get("ANNAS_DOWNLOAD_PATH", os.path.expanduser("~/Downloads/annas-archive")))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fetch(url: str) -> str | None:
    """Fetch a URL, INVALIDATING the cached mirror on failure.

    Previously a cached domain was permanent: once `_working_domain` was set, every later
    request went to it forever, and a bare `except: return None` erased the reason. So a mirror
    that started failing after selection failed for the remainder of the process lifetime with
    no cause and no failover. Clearing the cache lets the next call re-probe and move on.
    """
    global _working_domain
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        if _working_domain and _working_domain in url:
            _probe_report[_working_domain] = f"fetch-failed ({getattr(e, 'code', None) or getattr(e, 'reason', 'error')})"
            _working_domain = None
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
) -> str | None:
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
    if base is None:
        return None
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
    if url is None:
        return _upstream_error("search for books")
    if page > 1:
        url += f"&page={page}"

    html = _fetch(url)
    if not html:
        return _upstream_error("search for books")

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
    if url is None:
        return _upstream_error("search for journal articles")
    if page > 1:
        url += f"&page={page}"

    html = _fetch(url)
    if not html:
        return _upstream_error("search for journal articles")

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
    if base is None:
        return _upstream_error("fetch metadata")
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
    if base is None:
        return _upstream_error("request a fast download")
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


def _sanitize_path_component(s: str) -> str:
    """Clean a string for use as a directory or file name."""
    s = re.sub(r'[<>:"/\\|?*]', '_', s)
    s = re.sub(r'\s+', ' ', s).strip().rstrip('.')
    return s[:120] if s else "Unknown"


def _resolve_organized_dest(md5: str, base_dir: Path) -> tuple[Path, str | None, str | None]:
    """Look up metadata and return (dest_dir, author, title) for organized storage."""
    raw = info(md5)
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return base_dir, None, None

    if meta.get("error"):
        return base_dir, None, None

    author = meta.get("author", "").strip()
    title = meta.get("title", "").strip()

    if not author and not title:
        return base_dir, None, None

    author_dir = _sanitize_path_component(author) if author else "Unknown Author"
    title_dir = _sanitize_path_component(title) if title else md5

    dest = base_dir / author_dir / title_dir
    return dest, author, title


def _download_file(md5: str, output_dir: Path | None = None, path_index: int = 0, domain_index: int = 0, organize: bool = True) -> dict:
    """Internal: download a file via fast_download API with atomic write.

    When organize=True (default), files are saved to Author/Title/ subdirectories
    within the download path, using metadata from the info tool.
    """
    global _download_count

    key = _get_secret_key()
    if not key:
        return {"error": "AUTH_REQUIRED", "message": "ANNAS_SECRET_KEY not set"}

    params = {"md5": md5, "key": key, "path_index": path_index, "domain_index": domain_index}
    base = _get_base_url()
    if base is None:
        return json.loads(_upstream_error("request a fast download"))
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

    base_dest = output_dir or _get_download_path()
    author_name = None
    title_name = None

    if organize:
        dest, author_name, title_name = _resolve_organized_dest(md5, base_dest)
    else:
        dest = base_dest

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
            "author": author_name,
            "title": title_name,
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
            "author": author_name,
            "title": title_name,
            "filesize_bytes": filepath.stat().st_size,
            "downloads_this_session": _download_count,
        }
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return {"error": "DOWNLOAD_FAILED", "message": str(e), "md5": md5}


@mcp.tool()
def downloadBook(md5: str, output_path: str | None = None, path_index: int = 0, domain_index: int = 0, organize: bool = True) -> str:
    """Download a book by MD5 hash using the fast download API.

    By default, saves to Author/Title/ subdirectories within ANNAS_DOWNLOAD_PATH.
    Uses atomic writes (temp file + rename) to prevent partial files.
    Skips if the file already exists.

    Args:
        md5: 32-character MD5 hash from a search result
        output_path: Base directory to save file (default: ANNAS_DOWNLOAD_PATH)
        path_index: Storage path index (default: 0)
        domain_index: Download server index (default: 0)
        organize: Create Author/Title/ subdirectories (default: true)
    """
    dest = Path(output_path) if output_path else None
    result = _download_file(md5, output_dir=dest, path_index=path_index, domain_index=domain_index, organize=organize)
    return json.dumps(result)


@mcp.tool()
def downloadJournal(md5: str, output_path: str | None = None, organize: bool = True) -> str:
    """Download a journal article by MD5 hash.

    By default, saves to Author/Title/ subdirectories within ANNAS_DOWNLOAD_PATH.

    Args:
        md5: 32-character MD5 hash from a search result
        output_path: Base directory to save file (default: ANNAS_DOWNLOAD_PATH)
        organize: Create Author/Title/ subdirectories (default: true)
    """
    dest = Path(output_path) if output_path else None
    result = _download_file(md5, output_dir=dest, organize=organize)
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
    if base is None:
        return _upstream_error("reach SciDB")
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


@mcp.tool()
def lookupISBN(isbn: str) -> str:
    """Look up a book by ISBN-10 or ISBN-13 and return full metadata.

    Searches Anna's Archive for the exact ISBN, then enriches the top
    results with full metadata (title, author, publisher, year, description,
    cover URL, all identifiers, filesize, format, and download availability).

    Args:
        isbn: ISBN-10 (10 digits) or ISBN-13 (13 digits), with or without hyphens
    """
    clean = re.sub(r'[\s-]', '', isbn)
    if len(clean) == 10:
        isbn_type = "isbn10"
    elif len(clean) == 13:
        isbn_type = "isbn13"
    else:
        return json.dumps({"error": "INVALID_ISBN", "message": f"Expected 10 or 13 digits, got {len(clean)}", "isbn": isbn})

    base = _get_base_url()
    if base is None:
        return _upstream_error("search by ISBN")
    url = f"{base}/search?{urllib.parse.urlencode({'q': clean})}"
    html = _fetch(url)

    if not html:
        return json.dumps({"error": "UPSTREAM_UNAVAILABLE", "isbn": isbn})

    md5s = list(dict.fromkeys(re.findall(r'/md5/([a-f0-9]{32})', html)))
    if not md5s:
        return json.dumps({"error": "NOT_FOUND", "isbn": isbn, "isbn_type": isbn_type, "message": "No files found for this ISBN"})

    results = []
    for md5 in md5s[:5]:
        detail = json.loads(info(md5))
        if detail.get("error"):
            continue
        ids = detail.get("identifiers", {})
        matched_isbns = ids.get(isbn_type, [])
        if clean in matched_isbns:
            detail["isbn_match"] = "exact"
        else:
            all_isbns = ids.get("isbn13", []) + ids.get("isbn10", [])
            detail["isbn_match"] = "exact" if clean in all_isbns else "fuzzy"
        results.append(detail)

    results.sort(key=lambda r: (0 if r.get("isbn_match") == "exact" else 1))

    return json.dumps({
        "isbn": isbn,
        "isbn_type": isbn_type,
        "results": results,
        "count": len(results),
        "exact_matches": sum(1 for r in results if r.get("isbn_match") == "exact"),
    })


# ---------------------------------------------------------------------------
# Text extraction, search, and RAG integration
# ---------------------------------------------------------------------------

def _extract_epub_text(filepath: Path) -> str:
    """Extract plain text from an EPUB file using ebooklib + BeautifulSoup."""
    import warnings
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

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
