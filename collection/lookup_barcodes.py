#!/usr/bin/env python3
"""Normalize CD barcodes and look them up on MusicBrainz (resume-safe cache)."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

DIR = Path(__file__).resolve().parent
RAW_PATH = DIR / "barcodes.raw.txt"
BARCODES_PATH = DIR / "barcodes.txt"
NON_UPC_PATH = DIR / "non_upc.txt"
CACHE_PATH = DIR / "metadata.jsonl"
DISCOGS_TOKEN_PATH = DIR / ".discogs_token"
USER_AGENT = "flute-key-practice-collection/1.0 (local; contact: local user)"
MB_SEARCH = "https://musicbrainz.org/ws/2/release/"
MB_LOOKUP = "https://musicbrainz.org/ws/2/release/{mbid}"
DISCOGS_SEARCH = "https://api.discogs.com/database/search"
DISCOGS_RELEASE = "https://api.discogs.com/releases/{rid}"
MIN_INTERVAL = 1.05  # MusicBrainz courtesy rate limit
DISCOGS_INTERVAL = 1.05  # authenticated Discogs ~60/min


def normalize_code(raw: str) -> tuple[str, str]:
    """Return (normalized_or_raw, kind) where kind is upc|isbn|non_upc."""
    s = raw.strip()
    if not s:
        return "", "empty"
    if s.upper().startswith("U") and s[1:].isdigit():
        s = s[1:]
    if re.fullmatch(r"97[89]\d{10}", s):
        return s, "isbn"
    if re.fullmatch(r"\d{8,14}", s):
        return s, "upc"
    return s, "non_upc"


def barcode_variants(code: str) -> list[str]:
    """Try as-is and the common UPC↔EAN-13 zero-pad variant."""
    variants: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    add(code)
    if len(code) == 12:
        add("0" + code)
    elif len(code) == 13 and code.startswith("0"):
        add(code[1:])
    return variants


def load_raw_codes(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip()]


def write_normalized(raw_codes: list[str]) -> tuple[list[str], list[str], dict[str, int]]:
    counts: dict[str, int] = {}
    upc_order: list[str] = []
    non_upc: list[str] = []
    for raw in raw_codes:
        norm, kind = normalize_code(raw)
        if kind == "empty":
            continue
        counts[norm] = counts.get(norm, 0) + 1
        if counts[norm] > 1:
            continue
        if kind == "non_upc":
            non_upc.append(norm)
        else:
            upc_order.append(norm)

    BARCODES_PATH.write_text("\n".join(upc_order) + ("\n" if upc_order else ""), encoding="utf-8")
    NON_UPC_PATH.write_text("\n".join(non_upc) + ("\n" if non_upc else ""), encoding="utf-8")
    return upc_order, non_upc, counts


def load_cache(path: Path) -> dict[str, dict]:
    by_key: dict[str, dict] = {}
    if not path.exists():
        return by_key
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = obj.get("normalized") or obj.get("barcode")
            if key:
                by_key[key] = obj
    return by_key


def rewrite_cache(path: Path, records: dict[str, dict], order: list[str]) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for key in order:
            if key in records:
                f.write(json.dumps(records[key], ensure_ascii=False) + "\n")
        for key, rec in records.items():
            if key not in order:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(path)


def load_discogs_token() -> str | None:
    env = os.environ.get("DISCOGS_TOKEN", "").strip()
    if env:
        return env
    if DISCOGS_TOKEN_PATH.exists():
        return DISCOGS_TOKEN_PATH.read_text(encoding="utf-8").strip() or None
    return None


class HttpJsonClient:
    def __init__(self, interval: float, extra_headers: dict[str, str] | None = None) -> None:
        self._last = 0.0
        self._interval = interval
        self._extra = extra_headers or {}

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._interval - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get_json(self, url: str) -> dict | None:
        self._throttle()
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            **self._extra,
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(5.0 if e.code == 429 else 2.0)
                return self.get_json(url)
            raise


# Back-compat alias
MbClient = HttpJsonClient


def artist_credit_names(ac: list | None) -> list[str]:
    if not ac:
        return []
    names: list[str] = []
    for part in ac:
        name = part.get("name") or (part.get("artist") or {}).get("name")
        if name:
            names.append(name)
    return names


def extract_labels(release: dict) -> list[str]:
    labels: list[str] = []
    for info in release.get("label-info") or []:
        label = info.get("label") or {}
        name = label.get("name")
        if name:
            labels.append(name)
    return labels


def extract_formats(release: dict) -> list[str]:
    formats: list[str] = []
    for medium in release.get("media") or []:
        fmt = medium.get("format")
        if fmt:
            formats.append(fmt)
    return formats


def extract_tracks(release: dict) -> list[str]:
    titles: list[str] = []
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            rec = track.get("recording") or {}
            title = track.get("title") or rec.get("title")
            if title:
                titles.append(title)
    return titles


def extract_instrument_credits(release: dict) -> list[str]:
    """Collect instrument relationship attributes from release + recordings."""
    credits: list[str] = []
    seen: set[str] = set()

    def add_rels(entity: dict) -> None:
        for rel in entity.get("relations") or []:
            if rel.get("target-type") != "artist" and rel.get("type") not in (
                "instrument",
                "vocal",
                "performer",
                "orchestra",
                "conductor",
            ):
                # Still keep instrument-type relations
                pass
            attrs = rel.get("attributes") or []
            artist = (rel.get("artist") or {}).get("name") or ""
            rtype = rel.get("type") or ""
            if attrs:
                for attr in attrs:
                    line = f"{artist}: {attr}" if artist else str(attr)
                    if line not in seen:
                        seen.add(line)
                        credits.append(line)
            elif rtype in ("instrument", "performer") and artist:
                line = f"{artist}: {rtype}"
                if line not in seen:
                    seen.add(line)
                    credits.append(line)

    add_rels(release)
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            rec = track.get("recording") or {}
            add_rels(rec)
    return credits


def search_by_barcode(client: HttpJsonClient, barcode: str) -> tuple[dict | None, list[str]]:
    tried: list[str] = []
    for variant in barcode_variants(barcode):
        tried.append(variant)
        q = urllib.parse.urlencode(
            {
                "query": f"barcode:{variant}",
                "fmt": "json",
                "limit": "5",
            }
        )
        data = client.get_json(f"{MB_SEARCH}?{q}")
        releases = (data or {}).get("releases") or []
        if releases:
            # Prefer exact barcode match
            for rel in releases:
                if (rel.get("barcode") or "") == variant:
                    return rel, tried
            return releases[0], tried
    return None, tried


def enrich_release(client: HttpJsonClient, mbid: str) -> dict | None:
    # recordings give track titles; artist-rels give release-level instrument credits
    q = urllib.parse.urlencode(
        {
            "fmt": "json",
            "inc": "artists+labels+recordings+artist-credits+artist-rels",
        }
    )
    return client.get_json(f"{MB_LOOKUP.format(mbid=mbid)}?{q}")


def record_from_release(
    barcode: str,
    release: dict,
    tried: list[str],
    enriched: dict | None,
) -> dict:
    src = enriched or release
    tags = [t.get("name") for t in (src.get("tags") or []) if t.get("name")]
    return {
        "barcode": barcode,
        "normalized": barcode,
        "status": "ok",
        "title": src.get("title") or release.get("title"),
        "artists": artist_credit_names(src.get("artist-credit") or release.get("artist-credit")),
        "label": extract_labels(src),
        "date": src.get("date") or release.get("date"),
        "country": src.get("country") or release.get("country"),
        "format": extract_formats(src),
        "mbid": src.get("id") or release.get("id"),
        "url": f"https://musicbrainz.org/release/{src.get('id') or release.get('id')}",
        "discogs_id": None,
        "track_titles": extract_tracks(src),
        "instrument_credits": extract_instrument_credits(src),
        "tags": tags,
        "disambiguation": src.get("disambiguation") or release.get("disambiguation") or "",
        "raw_query_notes": tried,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "musicbrainz",
    }


def record_not_found(
    barcode: str,
    tried: list[str],
    status: str = "not_found",
    error: str | None = None,
    source: str = "musicbrainz",
) -> dict:
    return {
        "barcode": barcode,
        "normalized": barcode,
        "status": status,
        "title": None,
        "artists": [],
        "label": [],
        "date": None,
        "country": None,
        "format": [],
        "mbid": None,
        "url": None,
        "discogs_id": None,
        "track_titles": [],
        "instrument_credits": [],
        "tags": [],
        "disambiguation": "",
        "raw_query_notes": tried,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "error": error,
    }


def split_discogs_title(title: str) -> tuple[list[str], str]:
    """Discogs search titles are usually 'Artist - Release'."""
    if " - " in title:
        artist, _, rest = title.partition(" - ")
        return [artist.strip()], rest.strip()
    return [], title.strip()


def record_from_discogs(
    barcode: str,
    search_hit: dict,
    release: dict | None,
    tried: list[str],
) -> dict:
    title_raw = (release or {}).get("title") or search_hit.get("title") or ""
    if release and release.get("artists"):
        artists = [a.get("name") for a in release["artists"] if a.get("name")]
        title = release.get("title") or title_raw
    else:
        artists, title = split_discogs_title(title_raw)

    labels: list[str] = []
    if release:
        labels = [lab.get("name") for lab in (release.get("labels") or []) if lab.get("name")]
    elif search_hit.get("label"):
        labels = list(search_hit.get("label") or [])

    formats: list[str] = []
    if release:
        for fmt in release.get("formats") or []:
            name = fmt.get("name")
            if name:
                formats.append(name)
    elif search_hit.get("format"):
        formats = list(search_hit.get("format") or [])

    track_titles: list[str] = []
    instrument_credits: list[str] = []
    if release:
        for track in release.get("tracklist") or []:
            t = track.get("title")
            if t:
                track_titles.append(t)
            for extra in track.get("extraartists") or []:
                role = extra.get("role") or ""
                name = extra.get("name") or ""
                if name and role:
                    instrument_credits.append(f"{name}: {role}")
        for extra in release.get("extraartists") or []:
            role = extra.get("role") or ""
            name = extra.get("name") or ""
            if name and role:
                instrument_credits.append(f"{name}: {role}")

    rid = (release or {}).get("id") or search_hit.get("id")
    year = None
    if release and release.get("year"):
        year = str(release.get("year"))
    elif search_hit.get("year"):
        year = str(search_hit.get("year"))

    styles = list((release or {}).get("styles") or search_hit.get("style") or [])
    genres = list((release or {}).get("genres") or search_hit.get("genre") or [])

    return {
        "barcode": barcode,
        "normalized": barcode,
        "status": "ok",
        "title": title,
        "artists": artists,
        "label": labels,
        "date": year,
        "country": (release or {}).get("country") or search_hit.get("country"),
        "format": formats,
        "mbid": None,
        "url": f"https://www.discogs.com/release/{rid}" if rid else search_hit.get("uri"),
        "discogs_id": rid,
        "track_titles": track_titles,
        "instrument_credits": instrument_credits,
        "tags": styles + genres,
        "disambiguation": "",
        "raw_query_notes": tried,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "discogs",
    }


def search_discogs_barcode(client: HttpJsonClient, barcode: str) -> tuple[dict | None, list[str]]:
    tried: list[str] = []
    for variant in barcode_variants(barcode):
        tried.append(variant)
        q = urllib.parse.urlencode(
            {
                "barcode": variant,
                "type": "release",
                "per_page": "5",
            }
        )
        data = client.get_json(f"{DISCOGS_SEARCH}?{q}")
        results = (data or {}).get("results") or []
        if results:
            return results[0], tried
    return None, tried


def enrich_discogs(client: HttpJsonClient, release_id: int | str) -> dict | None:
    return client.get_json(DISCOGS_RELEASE.format(rid=release_id))


def seed_non_upc(records: dict[str, dict], non_upc: list[str]) -> None:
    for code in non_upc:
        if code in records and records[code].get("status") == "non_upc":
            continue
        records[code] = record_not_found(code, [], status="non_upc")


def lookup_all(force: bool = False, limit: int | None = None) -> None:
    if not RAW_PATH.exists():
        sys.exit(f"Missing {RAW_PATH}")

    raw_codes = load_raw_codes(RAW_PATH)
    upc_order, non_upc, counts = write_normalized(raw_codes)
    print(
        f"Raw lines: {len(raw_codes)}; unique UPC/ISBN: {len(upc_order)}; "
        f"non_upc: {len(non_upc)}; duplicates skipped: {sum(c - 1 for c in counts.values() if c > 1)}"
    )

    records = load_cache(CACHE_PATH)
    seed_non_upc(records, non_upc)
    rewrite_cache(CACHE_PATH, records, upc_order + non_upc)

    client = HttpJsonClient(MIN_INTERVAL)
    todo = upc_order if limit is None else upc_order[:limit]
    done = 0
    skipped = 0
    found = 0
    missing = 0

    for i, code in enumerate(todo, 1):
        existing = records.get(code)
        if (
            not force
            and existing
            and existing.get("status") in ("ok", "not_found")
            and existing.get("source") in ("musicbrainz", "discogs")
        ):
            skipped += 1
            continue
        if existing and existing.get("status") == "error" and not force:
            pass

        print(f"[{i}/{len(todo)}] {code} ...", end=" ", flush=True)
        try:
            hit, tried = search_by_barcode(client, code)
            if not hit:
                records[code] = record_not_found(code, tried)
                missing += 1
                print("not_found")
            else:
                mbid = hit.get("id")
                enriched = enrich_release(client, mbid) if mbid else None
                records[code] = record_from_release(code, hit, tried, enriched)
                found += 1
                title = records[code].get("title")
                print(f"ok — {title}")
        except Exception as e:
            records[code] = record_not_found(code, [], status="error", error=str(e))
            print(f"error — {e}")

        done += 1
        if done % 10 == 0:
            rewrite_cache(CACHE_PATH, records, upc_order + non_upc)

    rewrite_cache(CACHE_PATH, records, upc_order + non_upc)
    print(
        f"Finished MusicBrainz. looked_up={done} skipped={skipped} found={found} "
        f"not_found={missing} cache={CACHE_PATH}"
    )


def lookup_discogs_fallback(force: bool = False, limit: int | None = None) -> None:
    """Fill MusicBrainz not_found / error rows via Discogs barcode search."""
    token = load_discogs_token()
    if not token:
        sys.exit(
            f"No Discogs token. Set DISCOGS_TOKEN or create {DISCOGS_TOKEN_PATH}"
        )

    if not RAW_PATH.exists():
        sys.exit(f"Missing {RAW_PATH}")

    raw_codes = load_raw_codes(RAW_PATH)
    upc_order, non_upc, _counts = write_normalized(raw_codes)
    records = load_cache(CACHE_PATH)
    seed_non_upc(records, non_upc)

    client = HttpJsonClient(
        DISCOGS_INTERVAL,
        extra_headers={"Authorization": f"Discogs token={token}"},
    )

    todo: list[str] = []
    for code in upc_order:
        rec = records.get(code)
        if not rec:
            todo.append(code)
            continue
        if rec.get("status") == "ok" and not force:
            continue
        if rec.get("source") == "discogs" and rec.get("status") in ("ok", "not_found") and not force:
            continue
        if rec.get("status") in ("not_found", "error") or force:
            # Only retry MB misses (or forced)
            if force or rec.get("source") in ("musicbrainz", None) or rec.get("status") == "error":
                todo.append(code)

    if limit is not None:
        todo = todo[:limit]

    print(f"Discogs fallback candidates: {len(todo)}")
    found = 0
    missing = 0
    errors = 0

    for i, code in enumerate(todo, 1):
        print(f"[discogs {i}/{len(todo)}] {code} ...", end=" ", flush=True)
        try:
            hit, tried = search_discogs_barcode(client, code)
            if not hit:
                records[code] = record_not_found(code, tried, source="discogs")
                missing += 1
                print("not_found")
            else:
                rid = hit.get("id")
                enriched = enrich_discogs(client, rid) if rid else None
                records[code] = record_from_discogs(code, hit, enriched, tried)
                found += 1
                print(f"ok — {records[code].get('title')}")
        except Exception as e:
            records[code] = record_not_found(
                code, [], status="error", error=str(e), source="discogs"
            )
            errors += 1
            print(f"error — {e}")

        if i % 10 == 0:
            rewrite_cache(CACHE_PATH, records, upc_order + non_upc)

    rewrite_cache(CACHE_PATH, records, upc_order + non_upc)
    print(
        f"Finished Discogs. found={found} not_found={missing} errors={errors} cache={CACHE_PATH}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Re-fetch even if cached")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N barcodes")
    parser.add_argument(
        "--normalize-only",
        action="store_true",
        help="Only write barcodes.txt / non_upc.txt",
    )
    parser.add_argument(
        "--discogs-fallback",
        action="store_true",
        help="Look up MusicBrainz misses on Discogs (needs token)",
    )
    args = parser.parse_args()

    if args.normalize_only:
        raw_codes = load_raw_codes(RAW_PATH)
        upc_order, non_upc, counts = write_normalized(raw_codes)
        print(
            f"Wrote {BARCODES_PATH} ({len(upc_order)}) and {NON_UPC_PATH} ({len(non_upc)}); "
            f"dupes={sum(c - 1 for c in counts.values() if c > 1)}"
        )
        return

    if args.discogs_fallback:
        lookup_discogs_fallback(force=args.force, limit=args.limit)
        return

    lookup_all(force=args.force, limit=args.limit)


if __name__ == "__main__":
    main()
