#!/bin/sh
set -eu

DB_PATH="${FOIA_DB_PATH:-/data/foia_archive.db}"
FILES_DIR="${FOIA_FILES_DIR:-/data/files}"
DB_DIR="$(dirname "$DB_PATH")"

mkdir -p "$DB_DIR" "$FILES_DIR"
chown -R foia:foia "$DB_DIR" "$FILES_DIR"

case "${FOIA_BOOTSTRAP_DB_FROM_B2:-false}" in
  1|true|TRUE|yes|YES|on|ON)
    if [ ! -s "$DB_PATH" ]; then
      echo "No local archive database found; restoring newest verified B2 snapshot."
      gosu foia python /app/main.py bootstrap-db --destination "$DB_PATH"
    fi
    ;;
esac

exec gosu foia "$@"
