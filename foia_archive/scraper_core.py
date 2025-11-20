"""Generic scraper for FOIA reading rooms."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .storage import (
    document_exists,
    get_connection,
    insert_document,
    list_reading_rooms,
    update_download_metadata,
    update_reading_room_crawled,
)
from .utils import Config, clean_filename, logger


ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "zip"}


def get_reading_rooms_to_crawl(config: Config, limit: Optional[int] = None):
    conn = get_connection(config.storage.get("db_path"))
    rooms = list_reading_rooms(conn, limit=limit)
    conn.close()
    return rooms


def extract_document_links(html: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links: List[Dict[str, str]] = []
    for tag in soup.find_all("a", href=True):
        href = tag.get("href")
        if not href:
            continue
        absolute_url = urljoin(base_url, href)
        path = urlparse(absolute_url).path
        ext = path.split(".")[-1].lower() if "." in path else ""
        if ext in ALLOWED_EXTENSIONS:
            links.append({
                "url": absolute_url,
                "title": tag.get_text(strip=True) or href,
            })
    return links


def _save_file(content: bytes, url: str, files_dir: Path, filename_hint: str) -> Path:
    parsed = urlparse(url)
    ext = parsed.path.split(".")[-1] if "." in parsed.path else ""
    safe_name = clean_filename(filename_hint) or "document"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    filename = f"{digest}_{safe_name}"
    if ext:
        filename = f"{filename}.{ext}"
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
        path = urlparse(url).path
        ext = path.split(".")[-1].lower() if "." in path else ""
        filename_hint = path.split("/")[-1] or "document"

        if document_exists(conn, url):
            continue

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
        )

        if not dry_run:
            local_path = download_document(url, filename_hint, config)
            if local_path:
                update_download_metadata(conn, doc_id, str(local_path.relative_to(Path.cwd())), datetime.utcnow().isoformat())
        downloaded += 1

    update_reading_room_crawled(conn, rr_id, datetime.utcnow().isoformat())
    conn.close()
