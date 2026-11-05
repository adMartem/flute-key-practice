#!/usr/bin/env python3
"""Filter cached CD metadata for flute-featured releases (no network)."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
CACHE_PATH = DIR / "metadata.jsonl"
CONFIG_PATH = DIR / "filter_flute.yml"


def _parse_scalar(s: str):
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def load_simple_yaml_fixed(path: Path) -> dict:
    """Two-phase indent parser that handles map-or-list empty keys."""
    lines: list[tuple[int, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        lines.append((indent, raw_line.strip()))

    def parse_block(start: int, min_indent: int) -> tuple[dict, int]:
        result: dict = {}
        i = start
        while i < len(lines):
            indent, line = lines[i]
            if indent < min_indent:
                break
            if indent > min_indent and start != i:
                # Belongs to previous key; should have been consumed
                break
            if line.startswith("- "):
                raise ValueError(f"Orphan list item: {line}")
            if ":" not in line:
                i += 1
                continue
            key, _, rest = line.partition(":")
            key = key.strip()
            rest = rest.strip()
            i += 1
            if rest != "":
                result[key] = _parse_scalar(rest)
                continue
            # Decide list vs map from following lines
            if i >= len(lines) or lines[i][0] <= indent:
                result[key] = {}
                continue
            next_indent, next_line = lines[i]
            if next_line.startswith("- "):
                items: list = []
                while i < len(lines) and lines[i][0] == next_indent and lines[i][1].startswith("- "):
                    items.append(_parse_scalar(lines[i][1][2:].strip()))
                    i += 1
                result[key] = items
            else:
                nested, i = parse_block(i, next_indent)
                result[key] = nested
        return result, i

    root, _ = parse_block(0, 0)
    return root


def load_cache(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def compile_patterns(patterns: list[str] | None) -> list[re.Pattern]:
    return [re.compile(p) for p in (patterns or [])]


def searchable_blob(rec: dict) -> str:
    parts: list[str] = []
    if rec.get("title"):
        parts.append(str(rec["title"]))
    if rec.get("disambiguation"):
        parts.append(str(rec["disambiguation"]))
    parts.extend(rec.get("artists") or [])
    parts.extend(rec.get("track_titles") or [])
    parts.extend(rec.get("instrument_credits") or [])
    parts.extend(rec.get("tags") or [])
    parts.extend(rec.get("label") or [])
    return "\n".join(parts)


def any_match(text: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def list_field_match(values: list[str], patterns: list[re.Pattern]) -> bool:
    return any(any_match(v, patterns) for v in (values or []))


def classify(rec: dict, cfg: dict) -> str | None:
    if rec.get("status") != "ok":
        return None

    featured_cfg = cfg.get("featured") or {}
    candidates_cfg = cfg.get("candidates") or {}
    exclude_cfg = cfg.get("exclude") or {}

    title_pats = compile_patterns(featured_cfg.get("title_patterns"))
    artist_pats = compile_patterns(featured_cfg.get("artist_patterns"))
    instr_pats = compile_patterns(featured_cfg.get("instrument_patterns"))
    cand_pats = compile_patterns(candidates_cfg.get("keywords"))
    excl_feat = compile_patterns(exclude_cfg.get("featured_patterns"))
    excl_cand = compile_patterns(exclude_cfg.get("candidate_patterns"))

    title = rec.get("title") or ""
    artists = rec.get("artists") or []
    tracks = rec.get("track_titles") or []
    instruments = rec.get("instrument_credits") or []
    blob = searchable_blob(rec)

    featured = False
    if any_match(title, title_pats):
        featured = True
    if list_field_match(tracks, title_pats):
        featured = True
    if list_field_match(artists, artist_pats):
        featured = True
    if list_field_match(instruments, instr_pats):
        featured = True

    if featured and not any_match(blob, excl_feat):
        return "featured"

    if any_match(blob, cand_pats) and not any_match(blob, excl_cand):
        return "candidate"
    return None


def reason_for(rec: dict, cfg: dict) -> str:
    featured_cfg = cfg.get("featured") or {}
    title_pats = compile_patterns(featured_cfg.get("title_patterns"))
    artist_pats = compile_patterns(featured_cfg.get("artist_patterns"))
    instr_pats = compile_patterns(featured_cfg.get("instrument_patterns"))
    reasons: list[str] = []
    if any_match(rec.get("title") or "", title_pats):
        reasons.append("title")
    if list_field_match(rec.get("track_titles") or [], title_pats):
        reasons.append("tracks")
    if list_field_match(rec.get("artists") or [], artist_pats):
        reasons.append("artists")
    if list_field_match(rec.get("instrument_credits") or [], instr_pats):
        reasons.append("instruments")
    if not reasons:
        reasons.append("keyword")
    return ",".join(reasons)


def write_csv(path: Path, rows: list[dict], cfg: dict) -> None:
    fields = ["barcode", "title", "artists", "date", "label", "reason", "url", "mbid"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for rec in rows:
            w.writerow(
                {
                    "barcode": rec.get("normalized") or rec.get("barcode"),
                    "title": rec.get("title") or "",
                    "artists": "; ".join(rec.get("artists") or []),
                    "date": rec.get("date") or "",
                    "label": "; ".join(rec.get("label") or []),
                    "reason": reason_for(rec, cfg),
                    "url": rec.get("url") or "",
                    "mbid": rec.get("mbid") or "",
                }
            )


def write_md(path: Path, rows: list[dict], cfg: dict) -> None:
    lines = [
        "# Flute-featured CDs",
        "",
        "From Kim's dad's collection barcodes (MusicBrainz cache + local filter).",
        "",
    ]
    if not rows:
        lines.append("_No featured matches._")
    else:
        for rec in rows:
            artists = ", ".join(rec.get("artists") or []) or "Unknown artist"
            title = rec.get("title") or "(no title)"
            barcode = rec.get("normalized") or rec.get("barcode")
            url = rec.get("url") or ""
            reason = reason_for(rec, cfg)
            link = f" — [MusicBrainz]({url})" if url else ""
            lines.append(f"- **{title}** — {artists} (`{barcode}`, {reason}){link}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Directory for reports (default: same as cache parent)",
    )
    args = parser.parse_args()

    if not args.cache.exists():
        sys.exit(f"Missing cache {args.cache}; run lookup first")
    if not args.config.exists():
        sys.exit(f"Missing config {args.config}")

    outdir = args.outdir or args.cache.parent
    outdir.mkdir(parents=True, exist_ok=True)
    featured_csv = outdir / "flute_featured.csv"
    featured_md = outdir / "flute_featured.md"
    candidates_csv = outdir / "flute_candidates.csv"
    summary_path = outdir / "filter_summary.txt"

    cfg = load_simple_yaml_fixed(args.config)
    records = load_cache(args.cache)

    featured: list[dict] = []
    candidates: list[dict] = []
    status_counts: dict[str, int] = {}

    for rec in records:
        st = rec.get("status") or "unknown"
        status_counts[st] = status_counts.get(st, 0) + 1
        kind = classify(rec, cfg)
        if kind == "featured":
            featured.append(rec)
        elif kind == "candidate":
            candidates.append(rec)

    featured_keys = {(r.get("normalized") or r.get("barcode")) for r in featured}
    candidates = [
        r for r in candidates if (r.get("normalized") or r.get("barcode")) not in featured_keys
    ]

    featured.sort(key=lambda r: (r.get("title") or "").lower())
    candidates.sort(key=lambda r: (r.get("title") or "").lower())

    write_csv(featured_csv, featured, cfg)
    write_md(featured_md, featured, cfg)
    write_csv(candidates_csv, candidates, cfg)

    summary = [
        f"cache_records={len(records)}",
        f"status_counts={status_counts}",
        f"flute_featured={len(featured)}",
        f"flute_candidates={len(candidates)}",
        f"wrote {featured_csv}, {featured_md}, {candidates_csv}",
    ]
    text = "\n".join(summary) + "\n"
    summary_path.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
