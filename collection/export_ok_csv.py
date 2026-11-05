#!/usr/bin/env python3
"""Export all status=ok cache rows to CSV (unfiltered metadata dump)."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

DIR = Path(__file__).resolve().parent
CACHE_PATH = DIR / "metadata.jsonl"
OUT_PATH = DIR / "collection_ok.csv"


def join_list(values) -> str:
    if not values:
        return ""
    return "; ".join(str(v) for v in values)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    rows: list[dict] = []
    with args.cache.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("status") != "ok":
                continue
            rows.append(rec)

    rows.sort(
        key=lambda r: (
            (r.get("title") or "").lower(),
            r.get("normalized") or r.get("barcode") or "",
        )
    )

    fields = [
        "catalog",
        "barcode",
        "title",
        "artists",
        "date",
        "country",
        "label",
        "format",
        "source",
        "mbid",
        "discogs_id",
        "url",
        "disambiguation",
        "tags",
        "track_titles",
        "instrument_credits",
        "fetched_at",
    ]

    with args.out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for rec in rows:
            catalog = rec.get("catalog") or rec.get("normalized") or rec.get("barcode") or ""
            w.writerow(
                {
                    "catalog": catalog,
                    "barcode": rec.get("normalized") or rec.get("barcode") or catalog,
                    "title": rec.get("title") or "",
                    "artists": join_list(rec.get("artists")),
                    "date": rec.get("date") or "",
                    "country": rec.get("country") or "",
                    "label": join_list(rec.get("label")),
                    "format": join_list(rec.get("format")),
                    "source": rec.get("source") or "",
                    "mbid": rec.get("mbid") or "",
                    "discogs_id": rec.get("discogs_id") or "",
                    "url": rec.get("url") or "",
                    "disambiguation": rec.get("disambiguation") or "",
                    "tags": join_list(rec.get("tags")),
                    "track_titles": join_list(rec.get("track_titles")),
                    "instrument_credits": join_list(rec.get("instrument_credits")),
                    "fetched_at": rec.get("fetched_at") or "",
                }
            )

    print(f"wrote {args.out} rows={len(rows)}")


if __name__ == "__main__":
    main()
