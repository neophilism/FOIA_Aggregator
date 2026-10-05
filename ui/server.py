"""FastAPI UI for browsing FOIA archive documents."""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from foia_archive.storage import SORT_ORDERS, get_connection, init_db, query_documents_page
from foia_archive.utils import load_config

config = load_config("config/settings.yaml")
DB_PATH = Path(config.storage.get("db_path"))
FILES_DIR = Path(config.storage.get("files_dir"))
init_db(DB_PATH, FILES_DIR)

app = FastAPI(title="FOIA Archive")
templates = Jinja2Templates(directory="ui/templates")
app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")


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


@app.get("/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: Optional[str] = Query(None, max_length=200),
    agency_id: Optional[int] = Query(None),
    office_id: Optional[int] = Query(None),
    file_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    sort: str = Query("discovered_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    if sort not in SORT_ORDERS:
        sort = "discovered_desc"
    if page_size not in {25, 50, 100}:
        page_size = 50

    title_query = (q or "").strip() or None
    conn = get_db()
    try:
        agencies = fetch_agencies(conn)
        offices = fetch_offices(conn, agency_id)
        file_types = fetch_file_types(conn)
        documents, total_results = query_documents_page(
            conn,
            agency_id=agency_id,
            office_id=office_id,
            file_type=file_type,
            start_date=start_date,
            end_date=end_date,
            title_query=title_query,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        total_pages = max(1, math.ceil(total_results / page_size))
        if total_results and page > total_pages:
            page = total_pages
            documents, total_results = query_documents_page(
                conn,
                agency_id=agency_id,
                office_id=office_id,
                file_type=file_type,
                start_date=start_date,
                end_date=end_date,
                title_query=title_query,
                sort=sort,
                page=page,
                page_size=page_size,
            )
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
                agency_id=agency_id,
                office_id=office_id,
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
                agency_id=agency_id,
                office_id=office_id,
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
            "documents": documents,
            "title_query": title_query or "",
            "selected_agency": agency_id,
            "selected_office": office_id,
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
