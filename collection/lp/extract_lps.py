#!/usr/bin/env python3
"""Extract Label & Catalog# rows from JWG_LPs.numbers into catalogs.txt / rows.csv."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from numbers_parser import Document

DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = Path.home() / "Documents" / "JWG_LPs.numbers"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--outdir", type=Path, default=DIR)
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"Missing Numbers file: {args.source}")

    args.outdir.mkdir(parents=True, exist_ok=True)
    doc = Document(str(args.source))
    table = doc.sheets[0].tables[0]

    rows = []
    for r in range(1, table.num_rows):
        raw = table.cell(r, 0).value
        if raw is None:
            continue
        if isinstance(raw, float):
            cat = str(int(raw)) if raw == int(raw) else str(raw)
        else:
            cat = str(raw).strip()
        if not cat:
            continue
        rows.append(
            {
                "row": r,
                "catalog_raw": cat,
                "catalog_norm": re.sub(r"\s+", " ", cat).strip(),
            }
        )

    (args.outdir / "catalogs.raw.txt").write_text(
        "\n".join(r["catalog_raw"] for r in rows) + "\n", encoding="utf-8"
    )

    seen: dict[str, int] = {}
    unique: list[str] = []
    for r in rows:
        key = r["catalog_norm"].casefold()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 1:
            unique.append(r["catalog_norm"])

    (args.outdir / "catalogs.txt").write_text("\n".join(unique) + "\n", encoding="utf-8")

    with (args.outdir / "rows.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["row", "catalog_raw", "catalog_norm", "dup_count_key"])
        w.writeheader()
        for r in rows:
            w.writerow({**r, "dup_count_key": seen[r["catalog_norm"].casefold()]})

    meta = {
        "source_file": str(args.source),
        "sheet": doc.sheets[0].name,
        "table": table.name,
        "rows_with_catalog": len(rows),
        "unique_catalogs": len(unique),
        "duplicate_keys": sum(1 for v in seen.values() if v > 1),
        "notes": (
            "Front/Back photos are embedded in the Numbers file but not exposed as "
            "cell values via numbers-parser; keep the Numbers file for visual reference."
        ),
    }
    (args.outdir / "extract_summary.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
