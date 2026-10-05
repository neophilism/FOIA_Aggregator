#!/bin/sh
set -eu

DB_PATH="${FOIA_DB_PATH:-/data/foia_archive.db}"
FILES_DIR="${FOIA_FILES_DIR:-/data/files}"
DB_DIR="$(dirname "$DB_PATH")"

mkdir -p "$DB_DIR" "$FILES_DIR"
chown -R foia:foia "$DB_DIR" "$FILES_DIR"

exec gosu foia "$@"
