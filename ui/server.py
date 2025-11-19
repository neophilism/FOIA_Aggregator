"""FastAPI UI for browsing FOIA archive documents."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from foia_archive.storage import init_db
from foia_archive.utils import load_config

config = load_config("config/settings.yaml")
DB_PATH = Path(config.storage.get("db_path"))
FILES_DIR = Path(config.storage.get("files_dir"))
init_db(DB_PATH, FILES_DIR)

app = FastAPI(title="FOIA Archive")
templates = Jinja2Templates(directory="ui/templates")
app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def query_documents(
    conn: sqlite3.Connection,
    agency_id: Optional[int],
    office_id: Optional[int],
    file_type: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[sqlite3.Row]:
    query = [
        "SELECT d.id, d.title, d.file_type, d.published_date, d.discovered_at, d.local_path, d.url,",
        "       a.name AS agency_name, o.name AS office_name",
        "FROM documents d",
        "LEFT JOIN agencies a ON d.agency_id = a.id",
        "LEFT JOIN offices o ON d.office_id = o.id",
        "WHERE 1=1",
    ]
    params: List[Any] = []

    if agency_id:
        query.append("AND d.agency_id = ?")
        params.append(agency_id)
    if office_id:
        query.append("AND d.office_id = ?")
        params.append(office_id)
    if file_type:
        query.append("AND d.file_type = ?")
        params.append(file_type)
    if start_date:
        query.append("AND d.published_date >= ?")
        params.append(start_date)
    if end_date:
        query.append("AND d.published_date <= ?")
        params.append(end_date)

    query.append("ORDER BY d.discovered_at DESC LIMIT 200")
    sql = "\n".join(query)
    return conn.execute(sql, params).fetchall()


@app.get("/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    agency_id: Optional[int] = Query(None),
    office_id: Optional[int] = Query(None),
    file_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    conn = get_db()
    agencies = fetch_agencies(conn)
    offices = fetch_offices(conn, agency_id)
    file_types = fetch_file_types(conn)
    documents = query_documents(conn, agency_id, office_id, file_type, start_date, end_date)
    conn.close()
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "agencies": agencies,
            "offices": offices,
            "file_types": file_types,
            "documents": documents,
            "selected_agency": agency_id,
            "selected_office": office_id,
            "selected_file_type": file_type,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
