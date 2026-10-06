"""Generic scraper for FOIA reading rooms."""
from __future__ import annotations

import hashlib
import ipaddress
import os
import posixpath
import re
import socket
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, TypedDict
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .archive_storage import (
    ArchiveStorageError,
    B2ArchiveStorage,
    StorageQuotaReached,
    get_archive_storage,
)
from .pal_adapter import (
    build_search_params,
    cabinet_ids_from_page,
    document_download_url,
    is_pal_reading_room,
    parse_search_page,
    search_url as pal_search_url,
)
from .storage import (
    archived_remote_bytes,
    associate_document_source,
    get_connection,
    get_document_by_url,
    insert_document,
    list_reading_rooms,
    record_reading_room_crawl_failure,
    record_reading_room_crawl_success,
    update_document_published_date_if_missing,
    update_download_failure,
    update_download_metadata,
    upsert_document_text,
)
from .text_extraction import OCRSettings, extract_document_text
from .utils import Config, clean_filename, logger


ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "csv", "txt", "rtf",
    "ppt", "pptx", "zip", "xml", "json", "eml", "msg",
    "tif", "tiff", "jpg", "jpeg", "png", "mp3", "wav", "mp4", "mov",
}
DOCUMENT_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/csv": "csv",
    "text/plain": "txt",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/xml": "xml",
    "text/xml": "xml",
    "application/json": "json",
    "message/rfc822": "eml",
    "image/tiff": "tiff",
    "image/jpeg": "jpg",
    "image/png": "png",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}
HTML_MIME_TYPES = {"text/html", "application/xhtml+xml"}
SKIP_CRAWL_EXTENSIONS = {
    "css", "js", "ico", "svg", "woff", "woff2", "ttf", "eot",
}
PUBLISHED_DATE_ATTRS = (
    "data-published-date",
    "data-publication-date",
    "data-release-date",
)
PUBLISHED_DATE_HEADERS = {
    "date published",
    "date released",
    "publication date",
    "published",
    "published date",
    "release date",
    "released",
}
DATE_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y/%m/%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
)
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DOWNLOAD_CHUNK_SIZE = 64 * 1024
PAGE_CHUNK_SIZE = 64 * 1024
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


class UnsafeURL(ValueError):
    """Raised when a URL can target a non-public or unsupported destination."""


class SourceAccessBlocked(RuntimeError):
    """Public source returned an access-blocking response after compatibility retry."""


class FileTooLarge(ValueError):
    """Raised when a download exceeds the configured archive size limit."""


@dataclass(frozen=True)
class DownloadResult:
    status: str
    path: Optional[Path] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    sha256: Optional[str] = None
    error: Optional[str] = None


class DocumentLink(TypedDict):
    url: str
    title: str
    published_date: Optional[str]
    file_type: str


@dataclass(frozen=True)
class CrawlTarget:
    url: str
    depth: int
    title: str
    published_date: Optional[str] = None


@dataclass(frozen=True)
class CrawlScope:
    hostname: str
    port: Optional[int]
    path_prefix: str


class HostRateLimiter:
    """Apply a minimum delay between requests to the same host."""

    def __init__(self, delay_seconds: float):
        self.delay_seconds = max(0.0, float(delay_seconds))
        self._last_request: Dict[str, float] = {}

    def wait(self, url: str) -> None:
        if self.delay_seconds <= 0:
            return
        parsed = urlparse(url)
        key = parsed.netloc.lower()
        now = time.monotonic()
        last = self._last_request.get(key)
        if last is not None:
            remaining = self.delay_seconds - (now - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[key] = time.monotonic()


def _normalize_published_date(value: str) -> Optional[str]:
    """Normalize an explicitly supplied publication date to YYYY-MM-DD."""
    value = " ".join((value or "").split()).strip()
    if not value:
        return None

    iso_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(iso_value).date().isoformat()
    except ValueError:
        pass

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _explicit_date_attribute(tag) -> Optional[str]:
    for attr in PUBLISHED_DATE_ATTRS:
        raw_value = tag.get(attr)
        if isinstance(raw_value, str):
            normalized = _normalize_published_date(raw_value)
            if normalized:
                return normalized
    return None


def _published_date_from_table(anchor) -> Optional[str]:
    row = anchor.find_parent("tr")
    if row is None:
        return None
    table = row.find_parent("table")
    if table is None:
        return None

    rows = table.find_all("tr")
    try:
        row_index = rows.index(row)
    except ValueError:
        return None

    header_cells = None
    thead = table.find("thead")
    if thead is not None:
        header_rows = thead.find_all("tr")
        if header_rows:
            header_cells = header_rows[-1].find_all(["th", "td"], recursive=False)
    if not header_cells:
        for candidate in reversed(rows[:row_index]):
            candidate_cells = candidate.find_all(["th", "td"], recursive=False)
            header_count = len(candidate.find_all("th", recursive=False))
            if candidate_cells and header_count == len(candidate_cells):
                header_cells = candidate_cells
                break
    if not header_cells:
        return None

    cells = row.find_all(["th", "td"], recursive=False)
    for index, header in enumerate(header_cells):
        label = " ".join(header.get_text(" ", strip=True).lower().rstrip(":").split())
        if label not in PUBLISHED_DATE_HEADERS or index >= len(cells):
            continue
        cell = cells[index]
        time_tag = cell.find("time")
        if time_tag is not None:
            normalized = _normalize_published_date(
                time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
            )
            if normalized:
                return normalized
        return _normalize_published_date(cell.get_text(" ", strip=True))
    return None


def _is_publication_time(time_tag) -> bool:
    itemprop = (time_tag.get("itemprop") or "").lower()
    if itemprop == "datepublished":
        return True
    marker = " ".join(
        [
            time_tag.get("id") or "",
            " ".join(time_tag.get("class") or []),
            time_tag.get("title") or "",
            time_tag.get("aria-label") or "",
        ]
    ).lower()
    return any(token in marker for token in ("publish", "publication", "release", "released"))


def _published_date_from_context(anchor) -> Optional[str]:
    direct = _explicit_date_attribute(anchor)
    if direct:
        return direct

    table_date = _published_date_from_table(anchor)
    if table_date:
        return table_date

    container = anchor.find_parent(["tr", "li", "article"])
    if container is None:
        return None

    direct = _explicit_date_attribute(container)
    if direct:
        return direct

    candidates = []
    for node in container.find_all(True):
        candidate = _explicit_date_attribute(node)
        if candidate:
            candidates.append(candidate)
    unique_candidates = set(candidates)
    if len(unique_candidates) == 1:
        return next(iter(unique_candidates))
    if len(unique_candidates) > 1:
        return None

    time_candidates = []
    for time_tag in container.find_all("time"):
        if not _is_publication_time(time_tag):
            continue
        candidate = _normalize_published_date(
            time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
        )
        if candidate:
            time_candidates.append(candidate)
    unique_time_candidates = set(time_candidates)
    if len(unique_time_candidates) == 1:
        return next(iter(unique_time_candidates))
    return None


def _parse_utc_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _breadth_first_source_limit(rooms, limit: Optional[int]):
    """Prefer one eligible source per agency before adding agency duplicates."""
    if limit is None:
        return rooms
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        return rooms
    if limit_value <= 0 or len(rooms) <= limit_value:
        return rooms

    selected = []
    deferred = []
    seen_agencies = set()

    for room in rooms:
        agency_id = room["agency_id"]
        if agency_id is None:
            # Curated/shared roots without an agency ID should not all collapse
            # into one bucket; each remains independently eligible.
            group = ("room", room["id"])
        else:
            group = ("agency", agency_id)

        if group not in seen_agencies:
            selected.append(room)
            seen_agencies.add(group)
            if len(selected) >= limit_value:
                return selected
        else:
            deferred.append(room)

    for room in deferred:
        selected.append(room)
        if len(selected) >= limit_value:
            break
    return selected


def get_reading_rooms_to_crawl(config: Config, limit: Optional[int] = None):
    conn = get_connection(config.storage.get("db_path"))
    try:
        # Apply cooldown before presentation/source limiting so a failed root
        # does not occupy one of the bounded crawl slots.
        rooms = list_reading_rooms(conn, limit=None)
    finally:
        conn.close()

    try:
        cooldown_minutes = max(
            0.0,
            float(config.crawler.get("failed_source_retry_minutes", 60)),
        )
    except (TypeError, ValueError):
        logger.warning(
            "Invalid failed_source_retry_minutes; using 60-minute cooldown"
        )
        cooldown_minutes = 60.0

    if cooldown_minutes <= 0:
        return _breadth_first_source_limit(rooms, limit)

    now = datetime.now(timezone.utc)
    eligible = []
    skipped = 0
    for room in rooms:
        failed_at = _parse_utc_timestamp(room["last_error_at"])
        if failed_at is not None:
            age_minutes = max(
                0.0,
                (now - failed_at).total_seconds() / 60.0,
            )
            if age_minutes < cooldown_minutes:
                skipped += 1
                continue
        eligible.append(room)

    if skipped:
        logger.info(
            "Skipping %s recently failed reading rooms for %.1f-minute cooldown",
            skipped,
            cooldown_minutes,
        )
    return _breadth_first_source_limit(eligible, limit)


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False

        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return False
        try:
            return ipaddress.ip_address(hostname).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def _validate_public_destination(url: str) -> None:
    if not _is_http_url(url):
        raise UnsafeURL("Only credential-free HTTP(S) URLs are allowed")

    parsed = urlparse(url)
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeURL("URL contains an invalid port") from exc

    addresses = socket.getaddrinfo(
        parsed.hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    if not addresses:
        raise socket.gaierror(f"No addresses resolved for {parsed.hostname}")

    for result in addresses:
        address = result[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeURL(f"Resolved invalid IP address: {address}") from exc
        if not ip.is_global:
            raise UnsafeURL(
                f"URL resolves to a non-public address: {address}"
            )


def canonicalize_url(url: str) -> Optional[str]:
    """Normalize a crawl URL without changing its query semantics."""
    if not _is_http_url(url):
        return None
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    host_display = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host_display if port is None or default_port else f"{host_display}:{port}"

    raw_path = re.sub(r"/{2,}", "/", parsed.path or "/")
    had_trailing_slash = raw_path.endswith("/")
    path = posixpath.normpath(raw_path)
    if path == ".":
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    if had_trailing_slash and path != "/" and not path.endswith("/"):
        path += "/"

    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _extension_from_url(url: str) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _content_type(response) -> str:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Content-Type") or headers.get("content-type") or ""
    return raw.split(";", 1)[0].strip().lower()


def _content_disposition_filename(response) -> Optional[str]:
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    if not value:
        return None
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", value, flags=re.IGNORECASE)
    if not match:
        return None
    return unquote(match.group(1).strip().strip('"')) or None


def _document_type_from_response(response, url: str) -> Optional[str]:
    mime_type = _content_type(response)
    if mime_type in DOCUMENT_MIME_TYPES:
        return DOCUMENT_MIME_TYPES[mime_type]

    disposition_name = _content_disposition_filename(response)
    if disposition_name and "." in disposition_name:
        ext = disposition_name.rsplit(".", 1)[-1].lower()
        if ext in ALLOWED_EXTENSIONS:
            return ext

    ext = _extension_from_url(url)
    if ext in ALLOWED_EXTENSIONS and mime_type not in HTML_MIME_TYPES:
        return ext
    return None


def _filename_hint(url: str, title: str, file_type: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1] or ""
    extension = (
        name.rsplit(".", 1)[-1].lower()
        if "." in name
        else ""
    )
    if file_type and extension and extension not in ALLOWED_EXTENSIONS:
        name = clean_filename(title) or "document"
        expected_suffix = f".{file_type.lower()}"
        if not name.lower().endswith(expected_suffix):
            name = f"{name}{expected_suffix}"
    elif not name:
        name = clean_filename(title) or "document"
        if file_type:
            name = f"{name}.{file_type}"
    elif "." not in name and file_type:
        name = f"{name}.{file_type}"
    return clean_filename(name)


def _crawl_scope(root_url: str) -> CrawlScope:
    parsed = urlparse(root_url)
    path = parsed.path or "/"
    if path != "/" and not path.endswith("/") and "." in path.rsplit("/", 1)[-1]:
        path = path.rsplit("/", 1)[0] or "/"
    prefix = path.rstrip("/") or "/"
    return CrawlScope(
        hostname=(parsed.hostname or "").lower(),
        port=parsed.port,
        path_prefix=prefix,
    )


def _url_in_scope(url: str, scope: CrawlScope) -> bool:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if hostname != scope.hostname:
        return False
    if port != scope.port:
        return False
    path = parsed.path or "/"
    if scope.path_prefix == "/":
        return True
    return path == scope.path_prefix or path.startswith(scope.path_prefix + "/")


def extract_crawl_targets(
    html: str,
    base_url: str,
    scope: CrawlScope,
    depth: int,
) -> List[CrawlTarget]:
    """Return same-scope HTML/page candidates for bounded traversal."""
    soup = BeautifulSoup(html, "html.parser")
    targets: List[CrawlTarget] = []
    seen: Set[str] = set()

    candidates = list(soup.find_all("a", href=True))
    candidates.extend(
        tag for tag in soup.find_all("link", href=True)
        if "next" in {str(rel).lower() for rel in (tag.get("rel") or [])}
    )

    for tag in candidates:
        href = tag.get("href")
        if not href:
            continue
        canonical = canonicalize_url(urljoin(base_url, href))
        if not canonical or canonical in seen:
            continue

        ext = _extension_from_url(canonical)
        if ext in ALLOWED_EXTENSIONS or ext in SKIP_CRAWL_EXTENSIONS:
            continue
        if not _url_in_scope(canonical, scope):
            continue

        seen.add(canonical)
        targets.append(
            CrawlTarget(
                url=canonical,
                depth=depth,
                title=tag.get_text(" ", strip=True) or canonical,
                published_date=_published_date_from_context(tag),
            )
        )
    return targets


def extract_document_links(html: str, base_url: str) -> List[DocumentLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: List[DocumentLink] = []
    seen: Set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = tag.get("href")
        if not href:
            continue
        absolute_url = canonicalize_url(urljoin(base_url, href))
        if not absolute_url or absolute_url in seen:
            continue
        ext = _extension_from_url(absolute_url)
        if ext in ALLOWED_EXTENSIONS:
            seen.add(absolute_url)
            links.append(
                {
                    "url": absolute_url,
                    "title": tag.get_text(" ", strip=True) or href,
                    "published_date": _published_date_from_context(tag),
                    "file_type": ext,
                }
            )
    return links


def _archive_path(url: str, files_dir: Path, filename_hint: str) -> Path:
    parsed = urlparse(url)
    ext = parsed.path.rsplit(".", 1)[-1].lower() if "." in parsed.path else ""
    safe_name = clean_filename(filename_hint) or "document"
    if ext and not safe_name.lower().endswith(f".{ext}"):
        safe_name = f"{safe_name}.{ext}"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return files_dir / f"{digest}_{safe_name}"


def _save_file(content: bytes, url: str, files_dir: Path, filename_hint: str) -> Path:
    path = _archive_path(url, files_dir, filename_hint)
    with path.open("wb") as f:
        f.write(content)
    return path


def _close_response(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _request_headers(
    config: Config,
    *,
    browser_compatible: bool = False,
    referer: Optional[str] = None,
    xhr: bool = False,
) -> dict[str, str]:
    if browser_compatible:
        headers = {
            "User-Agent": config.crawler.get(
                "browser_user_agent",
                DEFAULT_BROWSER_USER_AGENT,
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    else:
        headers = {
            "User-Agent": config.crawler.get(
                "user_agent",
                "FOIAArchiveBot/0.1",
            )
        }
    if referer:
        headers["Referer"] = referer
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Accept"] = "text/html, */*; q=0.01"
    return headers


def _browser_fallback_enabled(config: Config) -> bool:
    value = config.crawler.get("browser_compatibility_fallback", True)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _request_with_safe_redirects(
    url: str,
    headers: dict,
    timeout: float,
    max_redirects: int,
    rate_limiter: Optional[HostRateLimiter] = None,
):
    current_url = url
    for redirect_count in range(max_redirects + 1):
        _validate_public_destination(current_url)
        if rate_limiter is not None:
            rate_limiter.wait(current_url)
        response = requests.get(
            current_url,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
        status_code = getattr(response, "status_code", 200)
        if status_code not in REDIRECT_STATUS_CODES:
            return response, current_url

        headers_map = getattr(response, "headers", {}) or {}
        location = headers_map.get("Location") or headers_map.get("location")
        _close_response(response)
        if not location:
            raise requests.HTTPError(
                f"Redirect response from {current_url} did not include Location"
            )
        if redirect_count >= max_redirects:
            raise requests.TooManyRedirects(
                f"Exceeded {max_redirects} redirects for {url}"
            )
        current_url = urljoin(current_url, location)

    raise requests.TooManyRedirects(f"Exceeded redirect limit for {url}")


def _stream_response_to_file(
    response,
    target_path: Path,
    max_bytes: int,
) -> tuple[int, str]:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > max_bytes:
            raise FileTooLarge(
                f"Content-Length {content_length} exceeds {max_bytes} bytes"
            )

    temp_path = None
    size = 0
    digest = hashlib.sha256()
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target_path.parent,
            prefix=".partial-",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLarge(
                        f"Download exceeded {max_bytes} bytes"
                    )
                temp_file.write(chunk)
                digest.update(chunk)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, target_path)
        temp_path = None
        return size, digest.hexdigest()
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _retry_delay(
    attempt: int,
    base_seconds: float,
    retry_after: Optional[str] = None,
    rate_limit_reset: Optional[str] = None,
    max_delay_seconds: float = 60.0,
) -> float:
    delay = max(0.0, float(base_seconds)) * (2 ** attempt)

    hinted_delay: Optional[float] = None
    if retry_after:
        try:
            hinted_delay = max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                hinted_delay = max(
                    0.0,
                    retry_at.timestamp() - datetime.now(timezone.utc).timestamp(),
                )
            except (TypeError, ValueError, OverflowError):
                hinted_delay = None

    if hinted_delay is None and rate_limit_reset:
        try:
            hinted_delay = max(0.0, float(rate_limit_reset) - time.time())
        except ValueError:
            hinted_delay = None

    if hinted_delay is not None:
        delay = max(delay, hinted_delay)

    return min(max(0.0, float(max_delay_seconds)), delay)


def _retry_sleep(
    attempt: int,
    base_seconds: float,
    retry_after: Optional[str] = None,
    rate_limit_reset: Optional[str] = None,
    max_delay_seconds: float = 60.0,
) -> None:
    delay = _retry_delay(
        attempt,
        base_seconds,
        retry_after=retry_after,
        rate_limit_reset=rate_limit_reset,
        max_delay_seconds=max_delay_seconds,
    )
    if delay > 0:
        time.sleep(delay)


def _read_limited_text(response, max_bytes: int) -> str:
    headers = getattr(response, "headers", {}) or {}
    content_length = headers.get("Content-Length") or headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None
        if declared is not None and declared > max_bytes:
            raise FileTooLarge(
                f"Page Content-Length {declared} exceeds {max_bytes} bytes"
            )

    if hasattr(response, "iter_content"):
        chunks: List[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=PAGE_CHUNK_SIZE):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise FileTooLarge(f"Page exceeded {max_bytes} bytes")
            chunks.append(chunk)
        raw = b"".join(chunks)
        encoding = getattr(response, "encoding", None) or "utf-8"
        return raw.decode(encoding, errors="replace")

    text_value = getattr(response, "text", "") or ""
    if len(text_value.encode("utf-8")) > max_bytes:
        raise FileTooLarge(f"Page exceeded {max_bytes} bytes")
    return text_value


def _fetch_crawl_resource(
    url: str,
    config: Config,
    rate_limiter: HostRateLimiter,
):
    headers = _request_headers(config)
    using_browser_fallback = False
    timeout = float(config.crawler.get("page_timeout_seconds", 30))
    max_redirects = int(config.downloader.get("max_redirects", 5))
    max_retries = int(config.downloader.get("max_retries", 3))
    backoff = float(config.downloader.get("retry_backoff_seconds", 1))
    max_retry_delay = float(
        config.downloader.get("max_retry_delay_seconds", 60)
    )

    for attempt in range(max_retries + 1):
        response = None
        try:
            response, final_url = _request_with_safe_redirects(
                url,
                headers=headers,
                timeout=timeout,
                max_redirects=max_redirects,
                rate_limiter=rate_limiter,
            )
            status_code = getattr(response, "status_code", 200)
            if (
                status_code == 403
                and _browser_fallback_enabled(config)
                and not using_browser_fallback
            ):
                logger.info(
                    "HTTP 403 from %s; retrying once with browser-compatible headers",
                    final_url,
                )
                _close_response(response)
                response = None
                headers = _request_headers(config, browser_compatible=True)
                using_browser_fallback = True
                response, final_url = _request_with_safe_redirects(
                    url,
                    headers=headers,
                    timeout=timeout,
                    max_redirects=max_redirects,
                    rate_limiter=rate_limiter,
                )
                status_code = getattr(response, "status_code", 200)
            if status_code == 403:
                raise SourceAccessBlocked(
                    f"HTTP 403 after browser-compatible fallback while crawling {final_url}"
                )
            if status_code in RETRYABLE_STATUS_CODES:
                headers_map = getattr(response, "headers", {}) or {}
                retry_after = headers_map.get("Retry-After") or headers_map.get("retry-after")
                rate_limit_reset = (
                    headers_map.get("X-RateLimit-Reset")
                    or headers_map.get("x-ratelimit-reset")
                    or headers_map.get("X-Rate-Limit-Reset")
                )
                _close_response(response)
                response = None
                if attempt < max_retries:
                    _retry_sleep(
                        attempt,
                        backoff,
                        retry_after=retry_after,
                        rate_limit_reset=rate_limit_reset,
                        max_delay_seconds=max_retry_delay,
                    )
                    continue
                raise requests.HTTPError(
                    f"HTTP {status_code} while crawling {final_url}"
                )
            if not 200 <= status_code < 300:
                raise requests.HTTPError(
                    f"HTTP {status_code} while crawling {final_url}"
                )
            return response, final_url
        except (UnsafeURL, FileTooLarge, SourceAccessBlocked):
            raise
        except (requests.RequestException, socket.gaierror):
            if response is not None:
                _close_response(response)
                response = None
            if attempt < max_retries:
                _retry_sleep(attempt, backoff)
                continue
            raise


def _fetch_pal_resource(
    session,
    method: str,
    url: str,
    config: Config,
    rate_limiter: HostRateLimiter,
    *,
    data: Optional[dict[str, str]] = None,
    referer: Optional[str] = None,
):
    """Fetch one known PAL endpoint without allowing unvalidated redirects."""
    headers = _request_headers(
        config,
        referer=referer,
        xhr=method.upper() == "POST",
    )
    using_browser_fallback = False

    timeout = float(config.crawler.get("page_timeout_seconds", 30))
    max_retries = int(config.downloader.get("max_retries", 3))
    backoff = float(config.downloader.get("retry_backoff_seconds", 1))
    max_retry_delay = float(
        config.downloader.get("max_retry_delay_seconds", 60)
    )

    for attempt in range(max_retries + 1):
        response = None
        try:
            _validate_public_destination(url)
            rate_limiter.wait(url)
            response = session.request(
                method.upper(),
                url,
                headers=headers,
                data=data,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
            status_code = getattr(response, "status_code", 200)
            if (
                status_code == 403
                and _browser_fallback_enabled(config)
                and not using_browser_fallback
            ):
                _close_response(response)
                response = None
                headers = _request_headers(
                    config,
                    browser_compatible=True,
                    referer=referer,
                    xhr=method.upper() == "POST",
                )
                using_browser_fallback = True
                rate_limiter.wait(url)
                response = session.request(
                    method.upper(),
                    url,
                    headers=headers,
                    data=data,
                    timeout=timeout,
                    stream=True,
                    allow_redirects=False,
                )
                status_code = getattr(response, "status_code", 200)
            if status_code == 403:
                raise SourceAccessBlocked(
                    f"HTTP 403 after browser-compatible fallback while crawling PAL endpoint {url}"
                )
            if status_code in REDIRECT_STATUS_CODES:
                headers_map = getattr(response, "headers", {}) or {}
                location = headers_map.get("Location") or headers_map.get("location")
                raise requests.HTTPError(
                    f"Unexpected redirect from PAL endpoint {url} to {location or 'unknown'}"
                )
            if status_code in RETRYABLE_STATUS_CODES:
                headers_map = getattr(response, "headers", {}) or {}
                retry_after = (
                    headers_map.get("Retry-After")
                    or headers_map.get("retry-after")
                )
                rate_limit_reset = (
                    headers_map.get("X-RateLimit-Reset")
                    or headers_map.get("x-ratelimit-reset")
                    or headers_map.get("X-Rate-Limit-Reset")
                )
                _close_response(response)
                response = None
                if attempt < max_retries:
                    _retry_sleep(
                        attempt,
                        backoff,
                        retry_after=retry_after,
                        rate_limit_reset=rate_limit_reset,
                        max_delay_seconds=max_retry_delay,
                    )
                    continue
                raise requests.HTTPError(
                    f"HTTP {status_code} while crawling PAL endpoint {url}"
                )
            if not 200 <= status_code < 300:
                raise requests.HTTPError(
                    f"HTTP {status_code} while crawling PAL endpoint {url}"
                )
            return response, url
        except (UnsafeURL, FileTooLarge, SourceAccessBlocked):
            raise
        except (requests.RequestException, socket.gaierror):
            if response is not None:
                _close_response(response)
                response = None
            if attempt < max_retries:
                _retry_sleep(attempt, backoff)
                continue
            raise


def download_document(
    url: str,
    filename_hint: str,
    config: Config,
    rate_limiter: Optional[HostRateLimiter] = None,
) -> DownloadResult:
    headers = _request_headers(config)
    using_browser_fallback = False
    files_dir = Path(config.storage.get("files_dir"))
    files_dir.mkdir(parents=True, exist_ok=True)

    timeout = float(config.downloader.get("timeout_seconds", 60))
    max_redirects = int(config.downloader.get("max_redirects", 5))
    max_retries = int(config.downloader.get("max_retries", 3))
    backoff = float(config.downloader.get("retry_backoff_seconds", 1))
    max_retry_delay = float(
        config.downloader.get("max_retry_delay_seconds", 60)
    )
    max_file_size_mb = float(config.downloader.get("max_file_size_mb", 100))
    max_bytes = max(1, int(max_file_size_mb * 1024 * 1024))

    for attempt in range(max_retries + 1):
        response = None
        try:
            response, final_url = _request_with_safe_redirects(
                url,
                headers=headers,
                timeout=timeout,
                max_redirects=max_redirects,
                rate_limiter=rate_limiter,
            )
            status_code = getattr(response, "status_code", 200)
            if (
                status_code == 403
                and _browser_fallback_enabled(config)
                and not using_browser_fallback
            ):
                _close_response(response)
                response = None
                headers = _request_headers(config, browser_compatible=True)
                using_browser_fallback = True
                response, final_url = _request_with_safe_redirects(
                    url,
                    headers=headers,
                    timeout=timeout,
                    max_redirects=max_redirects,
                    rate_limiter=rate_limiter,
                )
                status_code = getattr(response, "status_code", 200)
            if status_code == 403:
                return DownloadResult(
                    status="access_blocked",
                    error=(
                        "HTTP 403 after browser-compatible fallback while "
                        f"downloading {final_url}"
                    ),
                )
            if status_code in RETRYABLE_STATUS_CODES:
                error = f"HTTP {status_code} while downloading {final_url}"
                headers_map = getattr(response, "headers", {}) or {}
                retry_after = headers_map.get("Retry-After") or headers_map.get("retry-after")
                rate_limit_reset = (
                    headers_map.get("X-RateLimit-Reset")
                    or headers_map.get("x-ratelimit-reset")
                    or headers_map.get("X-Rate-Limit-Reset")
                )
                _close_response(response)
                response = None
                if attempt < max_retries:
                    _retry_sleep(
                        attempt,
                        backoff,
                        retry_after=retry_after,
                        rate_limit_reset=rate_limit_reset,
                        max_delay_seconds=max_retry_delay,
                    )
                    continue
                return DownloadResult(status="http_error", error=error)

            if not 200 <= status_code < 300:
                return DownloadResult(
                    status="http_error",
                    error=f"HTTP {status_code} while downloading {final_url}",
                )

            mime_type = _content_type(response) or None
            if mime_type in HTML_MIME_TYPES:
                return DownloadResult(
                    status="content_mismatch",
                    error=(
                        f"Expected an archive document at {url}, but "
                        f"{final_url} returned {mime_type}"
                    ),
                )

            target_path = _archive_path(url, files_dir, filename_hint)
            file_size, sha256 = _stream_response_to_file(
                response,
                target_path,
                max_bytes,
            )
            return DownloadResult(
                status="downloaded",
                path=target_path,
                mime_type=mime_type,
                file_size=file_size,
                sha256=sha256,
            )
        except UnsafeURL as exc:
            return DownloadResult(status="blocked_url", error=str(exc))
        except FileTooLarge as exc:
            return DownloadResult(status="too_large", error=str(exc))
        except socket.gaierror as exc:
            if attempt < max_retries:
                _retry_sleep(attempt, backoff)
                continue
            return DownloadResult(
                status="retryable_error",
                error=f"DNS resolution failed: {exc}",
            )
        except requests.RequestException as exc:
            if attempt < max_retries:
                _retry_sleep(attempt, backoff)
                continue
            return DownloadResult(
                status="retryable_error",
                error=str(exc),
            )
        except OSError as exc:
            return DownloadResult(status="io_error", error=str(exc))
        finally:
            if response is not None:
                _close_response(response)

    return DownloadResult(
        status="retryable_error",
        error="Download attempts exhausted",
    )


PERMANENT_DOWNLOAD_FAILURE_STATUSES = {
    "access_blocked",
    "blocked_url",
    "too_large",
    "content_mismatch",
    "storage_quota",
}


def _download_retry_due(
    existing,
    config: Config,
    *,
    missing_archive_file: bool = False,
) -> bool:
    if missing_archive_file:
        return True

    status = (existing["download_status"] or "").lower()
    if status in {"", "pending", "downloaded"}:
        return True

    attempted_at = _parse_utc_timestamp(existing["last_download_attempt_at"])
    if attempted_at is None:
        return True

    config_key = (
        "permanent_download_failure_retry_minutes"
        if status in PERMANENT_DOWNLOAD_FAILURE_STATUSES
        else "download_failure_retry_minutes"
    )
    try:
        cooldown_minutes = max(
            0.0,
            float(config.downloader.get(config_key, 0)),
        )
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s; disabling document cooldown for this run",
            config_key,
        )
        cooldown_minutes = 0.0

    if cooldown_minutes <= 0:
        return True

    age_minutes = max(
        0.0,
        (
            datetime.now(timezone.utc) - attempted_at
        ).total_seconds()
        / 60.0,
    )
    return age_minutes >= cooldown_minutes


def _candidate_counts_toward_source_limit(
    conn,
    url: str,
    *,
    dry_run: bool,
) -> bool:
    """Return whether a candidate should consume the per-source document quota.

    New records always count. In live mode, previously discovered records also
    count when they are not yet successfully archived. Already archived records
    do not consume quota, allowing bounded repeated crawl cycles to keep making
    progress instead of getting stuck on the same completed records.
    """
    canonical = canonicalize_url(url)
    if not canonical:
        return False

    existing = get_document_by_url(conn, canonical)
    if existing is None:
        return True
    if dry_run:
        return False

    has_archive_location = bool(
        existing["storage_key"] or existing["local_path"]
    )
    return (
        existing["download_status"] != "downloaded"
        or not has_archive_location
    )


def _process_document_candidate(
    conn,
    rr,
    url: str,
    title: str,
    published_date: Optional[str],
    file_type: str,
    config: Config,
    dry_run: bool,
    rate_limiter: HostRateLimiter,
) -> bool:
    """Persist/download one document candidate. Return True when newly discovered."""
    canonical = canonicalize_url(url)
    if not canonical:
        return False

    existing = get_document_by_url(conn, canonical)
    is_new = existing is None
    filename_hint = _filename_hint(canonical, title, file_type)

    missing_archive_file = False
    if existing:
        update_document_published_date_if_missing(conn, canonical, published_date)
        doc_id = existing["id"]
        associate_document_source(
            conn,
            doc_id,
            rr["id"],
            datetime.utcnow().isoformat(),
        )
        storage_backend = existing["storage_backend"] or (
            "local" if existing["local_path"] else None
        )
        storage_key = existing["storage_key"] or existing["local_path"]
        if storage_backend and storage_key:
            try:
                existing_storage = get_archive_storage(
                    config,
                    backend_name=storage_backend,
                )
                if existing_storage.exists(storage_key):
                    return False
            except ArchiveStorageError as exc:
                logger.warning(
                    "Could not verify stored archive object for %s: %s",
                    canonical,
                    exc,
                )
            missing_archive_file = True
            logger.warning(
                "Stored archive object missing or unverifiable for %s; "
                "retrying download",
                canonical,
            )
        elif not dry_run and not _download_retry_due(
            existing,
            config,
            missing_archive_file=missing_archive_file,
        ):
            logger.info(
                "Skipping recent failed download during cooldown: %s",
                canonical,
            )
            return False
    else:
        doc_id = insert_document(
            conn,
            url=canonical,
            title=title or canonical,
            file_type=file_type,
            filename=filename_hint,
            agency_id=rr["agency_id"],
            office_id=rr["office_id"],
            reading_room_id=rr["id"],
            discovered_at=datetime.utcnow().isoformat(),
            published_date=published_date,
        )

    if dry_run:
        return is_new

    try:
        archive_storage = get_archive_storage(config)
    except ArchiveStorageError as exc:
        update_download_failure(
            conn,
            doc_id,
            "storage_error",
            str(exc),
            datetime.utcnow().isoformat(),
        )
        return is_new

    if config.data.get("_storage_quota_reached"):
        update_download_failure(
            conn,
            doc_id,
            "storage_quota",
            "Archive storage safety cap reached; binary download deferred",
            datetime.utcnow().isoformat(),
        )
        return is_new

    if isinstance(archive_storage, B2ArchiveStorage):
        current_usage = archived_remote_bytes(conn, "b2")
        if (
            archive_storage.max_archive_bytes
            and current_usage >= archive_storage.max_archive_bytes
        ):
            config.data["_storage_quota_reached"] = True
            update_download_failure(
                conn,
                doc_id,
                "storage_quota",
                "B2 archive safety cap reached; binary download deferred",
                datetime.utcnow().isoformat(),
            )
            return is_new

    result = download_document(
        canonical,
        filename_hint,
        config,
        rate_limiter=rate_limiter,
    )
    if isinstance(result, Path):
        result = DownloadResult(status="downloaded", path=result)
    elif result is None:
        result = DownloadResult(
            status="retryable_error",
            error="Download failed",
        )

    attempted_at = datetime.utcnow().isoformat()
    if result.status == "downloaded" and result.path is not None:
        try:
            search_config = config.data.get("search") or {}
            try:
                max_indexed_chars = int(
                    search_config.get(
                        "max_indexed_chars_per_document",
                        5_000_000,
                    )
                )
            except (TypeError, ValueError):
                max_indexed_chars = 5_000_000
            max_indexed_chars = max(1_000, max_indexed_chars)
            effective_file_type = DOCUMENT_MIME_TYPES.get(
                result.mime_type or "",
                file_type,
            )
            extraction = extract_document_text(
                result.path,
                effective_file_type,
                max_chars=max_indexed_chars,
                ocr=OCRSettings.from_mapping(config.data.get("ocr")),
                legacy_office_timeout_seconds=float(
                    (config.data.get("search") or {}).get(
                        "legacy_office_timeout_seconds",
                        30,
                    )
                ),
            )

            current_usage = (
                archived_remote_bytes(conn, archive_storage.name)
                if archive_storage.name != "local"
                else 0
            )
            location = archive_storage.store_file(
                result.path,
                sha256=result.sha256,
                filename=filename_hint,
                mime_type=result.mime_type,
                current_usage_bytes=current_usage,
            )
            update_download_metadata(
                conn,
                doc_id,
                location.local_path,
                attempted_at,
                mime_type=result.mime_type,
                file_size=result.file_size,
                sha256=result.sha256,
                storage_backend=location.backend,
                storage_key=location.key,
            )
            upsert_document_text(
                conn,
                doc_id,
                body=extraction.text,
                extraction_status=extraction.status,
                extraction_error=extraction.error,
                extraction_method=extraction.method,
                extracted_at=attempted_at,
                character_count=extraction.character_count,
                truncated=extraction.truncated,
            )
        except StorageQuotaReached as exc:
            config.data["_storage_quota_reached"] = True
            try:
                result.path.unlink()
            except OSError:
                pass
            update_download_failure(
                conn,
                doc_id,
                "storage_quota",
                str(exc),
                attempted_at,
            )
        except ArchiveStorageError as exc:
            try:
                result.path.unlink()
            except OSError:
                pass
            update_download_failure(
                conn,
                doc_id,
                "storage_error",
                str(exc),
                attempted_at,
            )
    else:
        update_download_failure(
            conn,
            doc_id,
            result.status,
            result.error or "Download failed",
            attempted_at,
        )
    return is_new


def _crawl_pal_reading_room(
    conn,
    rr,
    root_url: str,
    config: Config,
    dry_run: bool,
    max_docs: Optional[int],
    rate_limiter: HostRateLimiter,
) -> None:
    """Crawl an AINS/PAL reading room through its public AJAX search endpoint."""
    max_pages = max(1, int(config.crawler.get("max_pages_per_source", 50)))
    max_discovered_docs = max(
        1,
        int(config.crawler.get("max_discovered_docs_per_source", 1000)),
    )
    page_max_bytes = max(
        1,
        int(float(config.crawler.get("page_max_size_mb", 5)) * 1024 * 1024),
    )

    session = requests.Session()
    seen_documents: Set[str] = set()
    new_documents = 0
    limited_documents = 0
    try:
        root_response = None
        try:
            root_response, _ = _fetch_pal_resource(
                session,
                "GET",
                root_url,
                config,
                rate_limiter,
            )
            root_html = _read_limited_text(root_response, page_max_bytes)
        finally:
            if root_response is not None:
                _close_response(root_response)

        cabinet_ids = cabinet_ids_from_page(root_html)
        if not cabinet_ids:
            raise ValueError("PAL reading room exposed no public cabinet IDs")

        endpoint = pal_search_url(root_url)
        page_index = 0
        total_pages = 1
        while page_index < min(total_pages, max_pages):
            response = None
            try:
                response, _ = _fetch_pal_resource(
                    session,
                    "POST",
                    endpoint,
                    config,
                    rate_limiter,
                    data=build_search_params(
                        cabinet_ids,
                        page_index=page_index,
                    ),
                    referer=root_url,
                )
                html = _read_limited_text(response, page_max_bytes)
                page = parse_search_page(html)
            except Exception:
                if page_index == 0:
                    raise
                logger.warning(
                    "PAL search page %s failed for %s; preserving partial crawl",
                    page_index + 1,
                    root_url,
                    exc_info=True,
                )
                break
            finally:
                if response is not None:
                    _close_response(response)

            if page_index == 0:
                total_pages = max(1, page.total_pages)

            for folder in page.folders:
                candidate_url = canonicalize_url(
                    document_download_url(root_url, folder)
                )
                if not candidate_url or candidate_url in seen_documents:
                    continue
                if len(seen_documents) >= max_discovered_docs:
                    logger.info(
                        "PAL document safety limit reached for %s after %s candidates",
                        root_url,
                        len(seen_documents),
                    )
                    record_reading_room_crawl_success(
                        conn,
                        rr["id"],
                        datetime.utcnow().isoformat(),
                    )
                    return
                consumes_quota = _candidate_counts_toward_source_limit(
                    conn,
                    candidate_url,
                    dry_run=dry_run,
                )
                if (
                    max_docs is not None
                    and consumes_quota
                    and limited_documents >= max(0, int(max_docs))
                ):
                    logger.info(
                        "Per-source document limit reached for %s",
                        root_url,
                    )
                    record_reading_room_crawl_success(
                        conn,
                        rr["id"],
                        datetime.utcnow().isoformat(),
                    )
                    return

                seen_documents.add(candidate_url)
                is_new = _process_document_candidate(
                    conn,
                    rr,
                    candidate_url,
                    folder.title,
                    folder.published_date,
                    "pdf",
                    config,
                    dry_run,
                    rate_limiter,
                )
                if is_new:
                    new_documents += 1
                if consumes_quota:
                    limited_documents += 1

            page_index += 1

        if total_pages > max_pages:
            logger.info(
                "PAL page limit reached for %s after %s of %s search pages",
                root_url,
                max_pages,
                total_pages,
            )

        record_reading_room_crawl_success(
            conn,
            rr["id"],
            datetime.utcnow().isoformat(),
        )
    except Exception as exc:
        attempted_at = datetime.utcnow().isoformat()
        error = f"{type(exc).__name__}: {exc}"[:2000]
        logger.warning("Failed to crawl PAL reading room %s: %s", root_url, exc)
        record_reading_room_crawl_failure(
            conn,
            rr["id"],
            attempted_at,
            error,
        )
    finally:
        session.close()


def _crawl_reading_room_with_connection(
    conn,
    rr_id: int,
    config: Config,
    dry_run: bool,
    max_docs: Optional[int],
    rate_limiter: Optional[HostRateLimiter] = None,
) -> None:
    rr = conn.execute(
        "SELECT * FROM reading_rooms WHERE id = ?",
        (rr_id,),
    ).fetchone()
    if not rr:
        logger.warning("Reading room %s not found", rr_id)
        return

    root_url = canonicalize_url(rr["url"])
    if not root_url:
        attempted_at = datetime.utcnow().isoformat()
        record_reading_room_crawl_failure(
            conn,
            rr_id,
            attempted_at,
            "Unsafe or invalid reading room URL",
        )
        return

    if rate_limiter is None:
        rate_limiter = HostRateLimiter(
            float(config.crawler.get("per_host_delay_seconds", 0))
        )

    if is_pal_reading_room(root_url):
        _crawl_pal_reading_room(
            conn,
            rr,
            root_url,
            config,
            dry_run,
            max_docs,
            rate_limiter,
        )
        return

    max_pages = max(1, int(config.crawler.get("max_pages_per_source", 50)))
    max_depth = max(0, int(config.crawler.get("max_depth", 3)))
    max_discovered_docs = max(
        1,
        int(config.crawler.get("max_discovered_docs_per_source", 1000)),
    )
    page_max_bytes = max(
        1,
        int(float(config.crawler.get("page_max_size_mb", 5)) * 1024 * 1024),
    )

    frontier: Deque[CrawlTarget] = deque(
        [
            CrawlTarget(
                url=root_url,
                depth=0,
                title=rr["label"] or root_url,
            )
        ]
    )
    queued_pages: Set[str] = {root_url}
    seen_pages: Set[str] = set()
    seen_documents: Set[str] = set()
    scope: Optional[CrawlScope] = None
    pages_fetched = 0
    new_documents = 0
    limited_documents = 0
    stop_for_document_limit = False

    while frontier and pages_fetched < max_pages and not stop_for_document_limit:
        target = frontier.popleft()
        if target.depth > max_depth or target.url in seen_pages:
            continue
        seen_pages.add(target.url)

        response = None
        try:
            response, final_url = _fetch_crawl_resource(
                target.url,
                config,
                rate_limiter,
            )
            final_url = canonicalize_url(final_url) or target.url

            if target.depth == 0:
                scope = _crawl_scope(final_url)
            elif scope is not None and not _url_in_scope(final_url, scope):
                logger.info(
                    "Skipping redirect outside reading-room scope: %s",
                    final_url,
                )
                continue

            pages_fetched += 1
            file_type = _document_type_from_response(response, final_url)
            if file_type:
                if final_url not in seen_documents:
                    consumes_quota = _candidate_counts_toward_source_limit(
                        conn,
                        final_url,
                        dry_run=dry_run,
                    )
                    if (
                        max_docs is not None
                        and consumes_quota
                        and limited_documents >= max(0, int(max_docs))
                    ):
                        logger.info(
                            "Per-source document limit reached for %s",
                            rr["url"],
                        )
                        break
                    if len(seen_documents) >= max_discovered_docs:
                        stop_for_document_limit = True
                        break
                    seen_documents.add(final_url)
                    is_new = _process_document_candidate(
                        conn,
                        rr,
                        final_url,
                        target.title,
                        target.published_date,
                        file_type,
                        config,
                        dry_run,
                        rate_limiter,
                    )
                    if is_new:
                        new_documents += 1
                    if consumes_quota:
                        limited_documents += 1
                if (
                    max_docs is not None
                    and limited_documents >= max(0, int(max_docs))
                ):
                    logger.info(
                        "Per-source document limit reached for %s",
                        rr["url"],
                    )
                    break
                continue

            mime_type = _content_type(response)
            if mime_type and mime_type not in HTML_MIME_TYPES:
                logger.info(
                    "Skipping unsupported crawl response %s (%s)",
                    final_url,
                    mime_type,
                )
                continue

            html = _read_limited_text(response, page_max_bytes)
        except Exception as exc:  # noqa: BLE001
            if target.depth == 0:
                attempted_at = datetime.utcnow().isoformat()
                error = f"{type(exc).__name__}: {exc}"[:2000]
                logger.warning("Failed to fetch reading room %s: %s", rr["url"], exc)
                record_reading_room_crawl_failure(
                    conn,
                    rr_id,
                    attempted_at,
                    error,
                )
                return
            logger.warning("Failed to crawl %s: %s", target.url, exc)
            continue
        finally:
            if response is not None:
                _close_response(response)

        for link in extract_document_links(html, final_url):
            url = link["url"]
            if url in seen_documents:
                continue
            consumes_quota = _candidate_counts_toward_source_limit(
                conn,
                url,
                dry_run=dry_run,
            )
            if (
                max_docs is not None
                and consumes_quota
                and limited_documents >= max(0, int(max_docs))
            ):
                logger.info(
                    "Per-source document limit reached for %s",
                    rr["url"],
                )
                stop_for_document_limit = True
                break
            if len(seen_documents) >= max_discovered_docs:
                stop_for_document_limit = True
                break

            seen_documents.add(url)
            is_new = _process_document_candidate(
                conn,
                rr,
                url,
                link.get("title") or url,
                link.get("published_date"),
                link["file_type"],
                config,
                dry_run,
                rate_limiter,
            )
            if is_new:
                new_documents += 1
            if consumes_quota:
                limited_documents += 1
            if (
                max_docs is not None
                and limited_documents >= max(0, int(max_docs))
            ):
                logger.info(
                    "Per-source document limit reached for %s",
                    rr["url"],
                )
                stop_for_document_limit = True
                break

        if stop_for_document_limit or target.depth >= max_depth or scope is None:
            continue

        for crawl_target in extract_crawl_targets(
            html,
            final_url,
            scope,
            target.depth + 1,
        ):
            if crawl_target.url in seen_pages or crawl_target.url in queued_pages:
                continue
            if len(queued_pages) >= max_pages:
                break
            queued_pages.add(crawl_target.url)
            frontier.append(crawl_target)

    if pages_fetched >= max_pages and frontier:
        logger.info(
            "Page limit reached for %s after %s fetched resources",
            rr["url"],
            pages_fetched,
        )
    if stop_for_document_limit:
        logger.info(
            "Document safety limit reached for %s after %s unique candidates",
            rr["url"],
            len(seen_documents),
        )

    record_reading_room_crawl_success(
        conn,
        rr_id,
        datetime.utcnow().isoformat(),
    )


def crawl_reading_room(
    rr_id: int,
    config: Config,
    dry_run: bool,
    max_docs: Optional[int],
    rate_limiter: Optional[HostRateLimiter] = None,
) -> None:
    """Crawl one source and always release its SQLite connection."""
    conn = get_connection(config.storage.get("db_path"))
    try:
        _crawl_reading_room_with_connection(
            conn,
            rr_id,
            config,
            dry_run,
            max_docs,
            rate_limiter=rate_limiter,
        )
    finally:
        conn.close()
