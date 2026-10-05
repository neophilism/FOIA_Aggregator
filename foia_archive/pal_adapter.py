"""Adapter helpers for AINS/PAL public FOIA reading rooms."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


PAL_READING_ROOM_HOSTS = {
    "efoia.cce.af.mil",
    "securefoia.ntsb.gov",
}
PAL_READING_ROOM_PATH = "/app/readingroom.aspx"
SHOW_DOCS_RE = re.compile(
    r"^javascript:showDocs\(\s*['\"](?P<id>\d+)['\"]\s*,\s*"
    r"['\"](?P<kind>[A-Za-z])['\"]\s*\);?$",
    flags=re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")


@dataclass(frozen=True)
class PalFolder:
    document_id: str
    document_kind: str
    title: str
    published_date: Optional[str] = None


@dataclass(frozen=True)
class PalSearchPage:
    folders: tuple[PalFolder, ...]
    page_indexes: tuple[int, ...]

    @property
    def total_pages(self) -> int:
        if not self.page_indexes:
            return 1
        return max(self.page_indexes) + 1


def is_pal_reading_room(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and (parsed.hostname or "").lower() in PAL_READING_ROOM_HOSTS
        and (parsed.path or "").lower().rstrip("/") == PAL_READING_ROOM_PATH
    )


def search_url(reading_room_url: str) -> str:
    return urljoin(reading_room_url, "SearchDocs.aspx")


def cabinet_ids_from_page(html: str) -> tuple[str, ...]:
    """Return PAL cabinet IDs in page order without duplicates."""
    soup = BeautifulSoup(html, "html.parser")
    result: list[str] = []
    seen: set[str] = set()
    for element in soup.find_all("input"):
        cabinet_id = (element.get("lang") or "").strip()
        onclick = element.get("onclick") or ""
        if not cabinet_id.isdigit() or "ChildClick" not in onclick:
            continue
        if cabinet_id in seen:
            continue
        seen.add(cabinet_id)
        result.append(cabinet_id)
    return tuple(result)


def build_search_params(
    cabinet_ids: Iterable[str],
    *,
    page_index: int = 0,
    filename: str = "*",
) -> dict[str, str]:
    ids = []
    seen = set()
    for value in cabinet_ids:
        value = str(value).strip()
        if not value.isdigit() or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    if not ids:
        raise ValueError("PAL search requires at least one cabinet ID")
    return {
        "doctypes": ",".join(ids),
        "filename": filename or "*",
        "sdate": "",
        "edate": "",
        "content": "",
        "sortBy": "",
        "sortOrder": "",
        "custom": '{"customFields":[]}',
        "pageIndex": str(max(0, int(page_index))),
    }


def _normalize_date_from_row(anchor) -> Optional[str]:
    row = anchor.find_parent("tr")
    if row is None:
        return None
    text = " ".join(row.stripped_strings)
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def parse_search_page(html: str) -> PalSearchPage:
    """Parse the HTML fragment returned by PAL SearchDocs.aspx."""
    soup = BeautifulSoup(html, "html.parser")
    folders: list[PalFolder] = []
    seen: set[tuple[str, str]] = set()

    for anchor in soup.find_all("a", href=True):
        match = SHOW_DOCS_RE.match((anchor.get("href") or "").strip())
        if not match:
            continue
        key = (match.group("id"), match.group("kind").upper())
        if key in seen:
            continue
        seen.add(key)
        title = " ".join(anchor.stripped_strings).strip()
        if not title:
            title = (anchor.get("title") or "").replace(
                "Click to view the Folder", ""
            ).strip()
        folders.append(
            PalFolder(
                document_id=key[0],
                document_kind=key[1],
                title=title or f"PAL folder {key[0]}",
                published_date=_normalize_date_from_row(anchor),
            )
        )

    page_indexes: list[int] = []
    page_select = soup.find(id="pageIndexOption")
    if page_select is not None:
        for option in page_select.find_all("option"):
            value = (option.get("value") or "").strip()
            if value.isdigit():
                page_indexes.append(int(value))

    return PalSearchPage(
        folders=tuple(folders),
        page_indexes=tuple(sorted(set(page_indexes))),
    )
