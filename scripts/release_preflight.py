#!/usr/bin/env python3
"""Evaluate local release evidence; never deploy or issue certification."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from foia_archive.release_readiness import evaluate_release

MAX_EVIDENCE_BYTES = 1024 * 1024

def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate evidence key")
        result[key] = value
    return result

def _reject_constant(_value):
    raise ValueError("Nonfinite evidence value")

def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate source-bound release evidence (no deployment).")
    parser.add_argument("evidence_file", type=Path, help="Provider-reviewed JSON evidence file")
    args = parser.parse_args(argv)
    try:
        evidence_file = args.evidence_file
        with evidence_file.open("rb") as handle:
            raw = handle.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ValueError("Evidence file larger than 1 MiB")
        evidence = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                              parse_constant=_reject_constant)
        assessment = evaluate_release(evidence)
    except (ValueError, OSError, TypeError, UnicodeError, json.JSONDecodeError):
        sys.stderr.write("Release evidence unavailable or invalid; no deployment authorized.\n")
        return 2
    sys.stdout.write(json.dumps(assessment, sort_keys=True) + "\n")
    return 0 if assessment["ready_for_review"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
