"""FastAPI UI for browsing FOIA archive documents."""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from foia_archive.archive_storage import ArchiveStorageError, LocalArchiveStorage, get_archive_storage
from foia_archive.storage import (
    SORT_ORDERS,
    get_archive_stats,
    get_connection,
    get_document_detail,
    get_document_source_details,
    init_db,
    query_document_snippets,
    query_documents_page,
)
from foia_archive.utils import load_config

config = load_config("config/settings.yaml")
DB_PATH = Path(config.storage.get("db_path"))
FILES_DIR = Path(config.storage.get("files_dir"))
init_db(DB_PATH, FILES_DIR)

app = FastAPI(title="FOIA Aggregator")
templates = Jinja2Templates(directory="ui/templates")
app.mount("/static", StaticFiles(directory="ui/static"), name="static")
app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")


def human_file_size(value: Optional[int]) -> str:
    if value is None:
        return "Unknown"
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(value)} B"


templates.env.filters["filesize"] = human_file_size


def get_db() -> sqlite3.Connection:
    return get_connection(DB_PATH)


def fetch_agencies(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM agencies ORDER BY name").fetchall()


def fetch_offices(conn: sqlite3.Connection, agency_id: Optional[int] = None) -> List[sqlite3.Row]:
    if agency_id:
        return conn.execute(
            "SELECT id, name FROM offices WHERE agency_id = ? ORDER BY name",
            (agency_id,),
        ).fetchall()
    return conn.execute("SELECT id, name FROM offices ORDER BY name").fetchall()


def fetch_file_types(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT DISTINCT file_type FROM documents WHERE file_type IS NOT NULL").fetchall()
    return [r[0] for r in rows if r[0]]


def _optional_int_filter(value: Optional[str], name: str) -> Optional[int]:
    """Normalize an optional select value from an HTML GET form.

    Browsers submit the "All ..." options as an empty string. Treat that as no
    filter rather than letting FastAPI reject the request before the search
    route can run.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be an integer when specified",
        )
    if parsed <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be a positive integer when specified",
        )
    return parsed


def _page_url(
    page: int,
    *,
    title_query: Optional[str],
    agency_id: Optional[int],
    office_id: Optional[int],
    file_type: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    sort: str,
    page_size: int,
) -> str:
    params = {
        "q": title_query or None,
        "agency_id": agency_id,
        "office_id": office_id,
        "file_type": file_type,
        "start_date": start_date,
        "end_date": end_date,
        "sort": sort if sort != "discovered_desc" else None,
        "page_size": page_size if page_size != 50 else None,
        "page": page if page != 1 else None,
    }
    return "/?" + urlencode(
        {key: value for key, value in params.items() if value not in (None, "")}
    )


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    conn = get_db()
    try:
        stats = get_archive_stats(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context={"stats": stats},
    )


@app.get("/record/{document_id}", response_class=HTMLResponse)
async def record_detail(request: Request, document_id: int):
    conn = get_db()
    try:
        document = get_document_detail(conn, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        sources = get_document_source_details(conn, document_id)
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request,
        name="record.html",
        context={
            "document": document,
            "sources": sources,
        },
    )


@app.get("/archive/{document_id}")
async def archived_document(document_id: int):
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id, local_path, storage_backend, storage_key, download_status
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if row["download_status"] != "downloaded":
        raise HTTPException(status_code=404, detail="Document is not archived")

    backend_name = row["storage_backend"] or (
        "local" if row["local_path"] else None
    )
    key = row["storage_key"] or row["local_path"]
    if not backend_name or not key:
        raise HTTPException(status_code=404, detail="Archive location is missing")

    try:
        backend = get_archive_storage(config, backend_name=backend_name)
        if isinstance(backend, LocalArchiveStorage):
            path = backend.path_for_key(key)
            if not path.is_file():
                raise HTTPException(status_code=404, detail="Archived file is missing")
            return FileResponse(path)
        return RedirectResponse(
            backend.presigned_url(key, expires_seconds=3600),
            status_code=307,
        )
    except HTTPException:
        raise
    except ArchiveStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Archive storage unavailable: {exc}",
        ) from exc


@app.get("/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: Optional[str] = Query(None, max_length=200),
    agency_id: Optional[str] = Query(None),
    office_id: Optional[str] = Query(None),
    file_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    sort: str = Query("discovered_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    agency_filter_id = _optional_int_filter(agency_id, "agency_id")
    office_filter_id = _optional_int_filter(office_id, "office_id")

    if sort not in SORT_ORDERS:
        sort = "discovered_desc"
    if page_size not in {25, 50, 100}:
        page_size = 50

    title_query = (q or "").strip() or None
    conn = get_db()
    try:
        agencies = fetch_agencies(conn)
        offices = fetch_offices(conn, agency_filter_id)
        file_types = fetch_file_types(conn)
        documents, total_results = query_documents_page(
            conn,
            agency_id=agency_filter_id,
            office_id=office_filter_id,
            file_type=file_type,
            start_date=start_date,
            end_date=end_date,
            title_query=title_query,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        stats = get_archive_stats(conn)
        total_pages = max(1, math.ceil(total_results / page_size))
        if total_results and page > total_pages:
            page = total_pages
            documents, total_results = query_documents_page(
                conn,
                agency_id=agency_filter_id,
                office_id=office_filter_id,
                file_type=file_type,
                start_date=start_date,
                end_date=end_date,
                title_query=title_query,
                sort=sort,
                page=page,
                page_size=page_size,
            )

        snippets = query_document_snippets(
            conn,
            [row["id"] for row in documents],
            title_query,
        )
        document_items = []
        for row in documents:
            item = dict(row)
            item["snippet"] = snippets.get(row["id"])
            document_items.append(item)
    finally:
        conn.close()

    first_result = (page - 1) * page_size + 1 if total_results else 0
    last_result = min(page * page_size, total_results)

    pagination = {
        "page": page,
        "total_pages": total_pages,
        "previous_url": (
            _page_url(
                page - 1,
                title_query=title_query,
                agency_id=agency_filter_id,
                office_id=office_filter_id,
                file_type=file_type,
                start_date=start_date,
                end_date=end_date,
                sort=sort,
                page_size=page_size,
            )
            if page > 1
            else None
        ),
        "next_url": (
            _page_url(
                page + 1,
                title_query=title_query,
                agency_id=agency_filter_id,
                office_id=office_filter_id,
                file_type=file_type,
                start_date=start_date,
                end_date=end_date,
                sort=sort,
                page_size=page_size,
            )
            if page < total_pages
            else None
        ),
    }

    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "agencies": agencies,
            "offices": offices,
            "file_types": file_types,
            "documents": document_items,
            "stats": stats,
            "filters_active": any(
                (
                    agency_filter_id,
                    office_filter_id,
                    file_type,
                    start_date,
                    end_date,
                    sort != "discovered_desc",
                    page_size != 50,
                )
            ),
            "title_query": title_query or "",
            "selected_agency": agency_filter_id,
            "selected_office": office_filter_id,
            "selected_file_type": file_type,
            "start_date": start_date,
            "end_date": end_date,
            "sort": sort,
            "page_size": page_size,
            "total_results": total_results,
            "first_result": first_result,
            "last_result": last_result,
            "pagination": pagination,
        },
    )
