"""Generic scraper for FOIA reading rooms."""
from __future__ import annotations

import hashlib
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
    update_document_published_date_if_missing,
    update_download_metadata,
    update_reading_room_crawled,
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


def extract_document_links(html: str, base_url: str) -> List[DocumentLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: List[DocumentLink] = []
    for tag in soup.find_all("a", href=True):
        href = tag.get("href")
        if not href:
            continue
        absolute_url = urljoin(base_url, href)
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


def _save_file(content: bytes, url: str, files_dir: Path, filename_hint: str) -> Path:
    parsed = urlparse(url)
    ext = parsed.path.rsplit(".", 1)[-1].lower() if "." in parsed.path else ""
    safe_name = clean_filename(filename_hint) or "document"
    if ext and not safe_name.lower().endswith(f".{ext}"):
        safe_name = f"{safe_name}.{ext}"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    filename = f"{digest}_{safe_name}"
    path = files_dir / filename
    with path.open("wb") as f:
        f.write(content)
    return path


def download_document(url: str, filename_hint: str, config: Config) -> Optional[Path]:
    headers = {"User-Agent": config.crawler.get("user_agent", "FOIAArchiveBot/0.1")}
    files_dir = Path(config.storage.get("files_dir"))
    files_dir.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        return _save_file(resp.content, url, files_dir, filename_hint)
    except Exception as exc:  # noqa: BLE001 - broad for logging
        logger.warning("Failed to download %s: %s", url, exc)
        return None


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
        logger.warning("Failed to fetch reading room %s: %s", rr["url"], exc)
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

        local_path = download_document(url, filename_hint, config)
        if local_path:
            files_dir = Path(config.storage.get("files_dir"))
            stored_path = local_path.relative_to(files_dir)
            update_download_metadata(
                conn,
                doc_id,
                stored_path.as_posix(),
                datetime.utcnow().isoformat(),
            )
        downloaded += 1

    update_reading_room_crawled(conn, rr_id, datetime.utcnow().isoformat())
    conn.close()
