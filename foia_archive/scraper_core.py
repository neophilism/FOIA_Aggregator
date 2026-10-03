"""Generic scraper for FOIA reading rooms."""
from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, TypedDict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .storage import (
    get_connection,
    get_document_by_url,
    insert_document,
    list_reading_rooms,
    record_reading_room_crawl_failure,
    record_reading_room_crawl_success,
    update_document_published_date_if_missing,
    update_download_failure,
    update_download_metadata,
)
from .utils import Config, clean_filename, logger


ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "zip"}
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


class UnsafeURL(ValueError):
    """Raised when a URL can target a non-public or unsupported destination."""


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


def get_reading_rooms_to_crawl(config: Config, limit: Optional[int] = None):
    conn = get_connection(config.storage.get("db_path"))
    rooms = list_reading_rooms(conn, limit=limit)
    conn.close()
    return rooms


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


def extract_document_links(html: str, base_url: str) -> List[DocumentLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: List[DocumentLink] = []
    for tag in soup.find_all("a", href=True):
        href = tag.get("href")
        if not href:
            continue
        absolute_url = urljoin(base_url, href)
        if not _is_http_url(absolute_url):
            continue
        path = urlparse(absolute_url).path
        ext = path.split(".")[-1].lower() if "." in path else ""
        if ext in ALLOWED_EXTENSIONS:
            links.append(
                {
                    "url": absolute_url,
                    "title": tag.get_text(strip=True) or href,
                    "published_date": _published_date_from_context(tag),
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


def _request_with_safe_redirects(
    url: str,
    headers: dict,
    timeout: float,
    max_redirects: int,
):
    current_url = url
    for redirect_count in range(max_redirects + 1):
        _validate_public_destination(current_url)
        response = requests.get(
            current_url,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
        if response.status_code not in REDIRECT_STATUS_CODES:
            return response, current_url

        location = response.headers.get("Location")
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


def _retry_sleep(attempt: int, base_seconds: float) -> None:
    if base_seconds <= 0:
        return
    time.sleep(base_seconds * (2 ** attempt))


def download_document(
    url: str,
    filename_hint: str,
    config: Config,
) -> DownloadResult:
    headers = {"User-Agent": config.crawler.get("user_agent", "FOIAArchiveBot/0.1")}
    files_dir = Path(config.storage.get("files_dir"))
    files_dir.mkdir(parents=True, exist_ok=True)

    timeout = float(config.downloader.get("timeout_seconds", 60))
    max_redirects = int(config.downloader.get("max_redirects", 5))
    max_retries = int(config.downloader.get("max_retries", 3))
    backoff = float(config.downloader.get("retry_backoff_seconds", 1))
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
            )
            status_code = response.status_code
            if status_code in RETRYABLE_STATUS_CODES:
                error = f"HTTP {status_code} while downloading {final_url}"
                _close_response(response)
                response = None
                if attempt < max_retries:
                    _retry_sleep(attempt, backoff)
                    continue
                return DownloadResult(status="http_error", error=error)

            if not 200 <= status_code < 300:
                return DownloadResult(
                    status="http_error",
                    error=f"HTTP {status_code} while downloading {final_url}",
                )

            target_path = _archive_path(url, files_dir, filename_hint)
            file_size, sha256 = _stream_response_to_file(
                response,
                target_path,
                max_bytes,
            )
            content_type = response.headers.get("Content-Type")
            mime_type = (
                content_type.split(";", 1)[0].strip().lower()
                if content_type
                else None
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


def crawl_reading_room(rr_id: int, config: Config, dry_run: bool, max_docs: Optional[int]) -> None:
    conn = get_connection(config.storage.get("db_path"))
    rr = conn.execute(
        "SELECT * FROM reading_rooms WHERE id = ?",
        (rr_id,),
    ).fetchone()
    if not rr:
        logger.warning("Reading room %s not found", rr_id)
        conn.close()
        return

    headers = {"User-Agent": config.crawler.get("user_agent", "FOIAArchiveBot/0.1")}
    try:
        resp = requests.get(rr["url"], headers=headers, timeout=60)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        attempted_at = datetime.utcnow().isoformat()
        error = f"{type(exc).__name__}: {exc}"[:2000]
        logger.warning("Failed to fetch reading room %s: %s", rr["url"], exc)
        record_reading_room_crawl_failure(
            conn,
            rr_id,
            attempted_at,
            error,
        )
        conn.close()
        return

    links = extract_document_links(resp.text, rr["url"])
    logger.info("Found %s candidate documents at %s", len(links), rr["url"])

    downloaded = 0
    for link in links:
        url = link["url"]
        title = link.get("title") or url
        published_date = link.get("published_date")
        path = urlparse(url).path
        ext = path.split(".")[-1].lower() if "." in path else ""
        filename_hint = path.split("/")[-1] or "document"

        existing = get_document_by_url(conn, url)
        if existing:
            update_document_published_date_if_missing(conn, url, published_date)
            doc_id = existing["id"]
            if existing["local_path"]:
                files_dir = Path(config.storage.get("files_dir"))
                archived_path = files_dir / existing["local_path"]
                if archived_path.is_file():
                    continue
                logger.warning(
                    "Stored file missing for %s; retrying download",
                    url,
                )
        else:
            if dry_run and max_docs is not None and downloaded >= max_docs:
                logger.info("Dry run limit reached for %s", rr["url"])
                break

            discovered_at = datetime.utcnow().isoformat()
            doc_id = insert_document(
                conn,
                url=url,
                title=title,
                file_type=ext,
                filename=filename_hint,
                agency_id=rr["agency_id"],
                office_id=rr["office_id"],
                reading_room_id=rr_id,
                discovered_at=discovered_at,
                published_date=published_date,
            )

        if dry_run:
            downloaded += 1
            continue

        result = download_document(url, filename_hint, config)
        if isinstance(result, Path):
            result = DownloadResult(status="downloaded", path=result)
        elif result is None:
            result = DownloadResult(
                status="retryable_error",
                error="Download failed",
            )

        attempted_at = datetime.utcnow().isoformat()
        if result.status == "downloaded" and result.path is not None:
            files_dir = Path(config.storage.get("files_dir"))
            stored_path = result.path.relative_to(files_dir)
            update_download_metadata(
                conn,
                doc_id,
                stored_path.as_posix(),
                attempted_at,
                mime_type=result.mime_type,
                file_size=result.file_size,
                sha256=result.sha256,
            )
        else:
            update_download_failure(
                conn,
                doc_id,
                result.status,
                result.error or "Download failed",
                attempted_at,
            )
        downloaded += 1

    record_reading_room_crawl_success(
        conn,
        rr_id,
        datetime.utcnow().isoformat(),
    )
    conn.close()
