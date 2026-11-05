#!/usr/bin/env python3
"""Look up LP catalog numbers via Discogs (primary) and MusicBrainz (fallback)."""

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
COLLECTION = DIR.parent
CATALOGS_PATH = DIR / "catalogs.txt"
CACHE_PATH = DIR / "metadata.jsonl"
DISCOGS_TOKEN_PATH = COLLECTION / ".discogs_token"
USER_AGENT = "flute-key-practice-lp-collection/1.0 (local; contact: local user)"
DISCOGS_SEARCH = "https://api.discogs.com/database/search"
DISCOGS_RELEASE = "https://api.discogs.com/releases/{rid}"
MB_SEARCH = "https://musicbrainz.org/ws/2/release/"
MB_LOOKUP = "https://musicbrainz.org/ws/2/release/{mbid}"
DISCOGS_INTERVAL = 1.05
MB_INTERVAL = 1.05


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
            with urllib.request.urlopen(req, timeout=45, context=SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(5.0 if e.code == 429 else 2.0)
                return self.get_json(url)
            raise


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
            key = obj.get("catalog_key") or obj.get("catalog")
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


def catalog_key(cat: str) -> str:
    return re.sub(r"\s+", " ", cat.strip()).casefold()


def catalog_variants(cat: str) -> list[str]:
    """Variants for catno search."""
    base = re.sub(r"\s+", " ", cat.strip())
    variants: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        v = v.strip()
        if not v:
            return
        k = v.casefold()
        if k not in seen:
            seen.add(k)
            variants.append(v)

    add(base)
    add(base.upper())
    add(base.replace(" ", "-"))
    add(base.replace("-", " "))
    add(re.sub(r"[\s\-]+", "", base))
    add(re.sub(r"[\s\-]+", "-", base))
    # Digits with spaces like "2530 716" → "2530 716" / "2530716" / "2530-716"
    if re.search(r"\d", base):
        digits_spaced = re.sub(r"\s+", " ", base)
        add(digits_spaced)
    return variants


def split_discogs_title(title: str) -> tuple[list[str], str]:
    if " - " in title:
        artist, _, rest = title.partition(" - ")
        return [artist.strip()], rest.strip()
    return [], title.strip()


def empty_record(catalog: str, status: str, source: str, tried: list[str], error: str | None = None) -> dict:
    return {
        "catalog": catalog,
        "catalog_key": catalog_key(catalog),
        "status": status,
        "title": None,
        "artists": [],
        "label": [],
        "date": None,
        "country": None,
        "format": [],
        "mbid": None,
        "discogs_id": None,
        "url": None,
        "track_titles": [],
        "instrument_credits": [],
        "tags": [],
        "disambiguation": "",
        "raw_query_notes": tried,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "error": error,
        # filter_collection compatibility
        "barcode": catalog,
        "normalized": catalog,
    }


def record_from_discogs(catalog: str, search_hit: dict, release: dict | None, tried: list[str]) -> dict:
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
        "catalog": catalog,
        "catalog_key": catalog_key(catalog),
        "status": "ok",
        "title": title,
        "artists": artists,
        "label": labels,
        "date": year,
        "country": (release or {}).get("country") or search_hit.get("country"),
        "format": formats,
        "mbid": None,
        "discogs_id": rid,
        "url": f"https://www.discogs.com/release/{rid}" if rid else search_hit.get("uri"),
        "track_titles": track_titles,
        "instrument_credits": instrument_credits,
        "tags": styles + genres,
        "disambiguation": "",
        "raw_query_notes": tried,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "discogs",
        "error": None,
        "barcode": catalog,
        "normalized": catalog,
    }


def artist_credit_names(ac: list | None) -> list[str]:
    if not ac:
        return []
    names: list[str] = []
    for part in ac:
        name = part.get("name") or (part.get("artist") or {}).get("name")
        if name:
            names.append(name)
    return names


def record_from_mb(catalog: str, release: dict, enriched: dict | None, tried: list[str]) -> dict:
    src = enriched or release
    labels: list[str] = []
    for info in src.get("label-info") or []:
        label = (info.get("label") or {}).get("name")
        if label:
            labels.append(label)
    formats: list[str] = []
    for medium in src.get("media") or []:
        fmt = medium.get("format")
        if fmt:
            formats.append(fmt)
    tracks: list[str] = []
    for medium in src.get("media") or []:
        for track in medium.get("tracks") or []:
            title = track.get("title") or (track.get("recording") or {}).get("title")
            if title:
                tracks.append(title)
    mbid = src.get("id") or release.get("id")
    return {
        "catalog": catalog,
        "catalog_key": catalog_key(catalog),
        "status": "ok",
        "title": src.get("title") or release.get("title"),
        "artists": artist_credit_names(src.get("artist-credit") or release.get("artist-credit")),
        "label": labels,
        "date": src.get("date") or release.get("date"),
        "country": src.get("country") or release.get("country"),
        "format": formats,
        "mbid": mbid,
        "discogs_id": None,
        "url": f"https://musicbrainz.org/release/{mbid}" if mbid else None,
        "track_titles": tracks,
        "instrument_credits": [],
        "tags": [],
        "disambiguation": src.get("disambiguation") or "",
        "raw_query_notes": tried,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "musicbrainz",
        "error": None,
        "barcode": catalog,
        "normalized": catalog,
    }


def catno_match_score(query: str, result_catno: str | None) -> int:
    if not result_catno:
        return 0
    a = re.sub(r"[\s\-]+", "", query).casefold()
    b = re.sub(r"[\s\-]+", "", result_catno).casefold()
    if a == b:
        return 100
    if a in b or b in a:
        return 70
    return 10


def prefer_vinyl(results: list[dict]) -> list[dict]:
    vinyl = []
    other = []
    for r in results:
        formats = " ".join(r.get("format") or []).lower()
        if "vinyl" in formats or "lp" in formats:
            vinyl.append(r)
        else:
            other.append(r)
    return vinyl + other


def search_discogs(client: HttpJsonClient, catalog: str) -> tuple[dict | None, list[str]]:
    tried: list[str] = []
    best: dict | None = None
    best_score = -1
    for variant in catalog_variants(catalog)[:4]:
        tried.append(f"discogs:{variant}")
        q = urllib.parse.urlencode(
            {
                "catno": variant,
                "type": "release",
                "format": "Vinyl",
                "per_page": "10",
            }
        )
        data = client.get_json(f"{DISCOGS_SEARCH}?{q}")
        results = prefer_vinyl((data or {}).get("results") or [])
        if not results:
            # retry without format filter
            q2 = urllib.parse.urlencode(
                {
                    "catno": variant,
                    "type": "release",
                    "per_page": "10",
                }
            )
            tried.append(f"discogs-any:{variant}")
            data = client.get_json(f"{DISCOGS_SEARCH}?{q2}")
            results = prefer_vinyl((data or {}).get("results") or [])
        for r in results:
            score = catno_match_score(catalog, r.get("catno"))
            # boost vinyl
            formats = " ".join(r.get("format") or []).lower()
            if "vinyl" in formats or "lp" in formats:
                score += 5
            if score > best_score:
                best_score = score
                best = r
        if best_score >= 100:
            break
    if best and best_score >= 70:
        return best, tried
    # weak match: still return best if any, but caller may reject
    return (best if best_score >= 70 else None), tried


def search_musicbrainz(client: HttpJsonClient, catalog: str) -> tuple[dict | None, list[str]]:
    tried: list[str] = []
    for variant in catalog_variants(catalog)[:3]:
        tried.append(f"mb:{variant}")
        # escape Lucene special chars lightly
        safe = variant.replace(":", "\\:")
        q = urllib.parse.urlencode(
            {
                "query": f'catno:"{safe}"',
                "fmt": "json",
                "limit": "5",
            }
        )
        data = client.get_json(f"{MB_SEARCH}?{q}")
        releases = (data or {}).get("releases") or []
        if releases:
            # prefer vinyl / 12" vinyl
            def mb_score(rel: dict) -> int:
                score = 0
                for m in rel.get("media") or []:
                    fmt = (m.get("format") or "").lower()
                    if "vinyl" in fmt:
                        score += 10
                return score

            releases = sorted(releases, key=mb_score, reverse=True)
            return releases[0], tried
    return None, tried


def lookup_all(force: bool = False, limit: int | None = None) -> None:
    token = load_discogs_token()
    if not token:
        sys.exit(f"Missing Discogs token ({DISCOGS_TOKEN_PATH} or DISCOGS_TOKEN)")
    if not CATALOGS_PATH.exists():
        sys.exit(f"Missing {CATALOGS_PATH}")

    catalogs = [ln.strip() for ln in CATALOGS_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if limit is not None:
        catalogs = catalogs[:limit]

    order = [catalog_key(c) for c in catalogs]
    records = load_cache(CACHE_PATH)
    discogs = HttpJsonClient(
        DISCOGS_INTERVAL,
        extra_headers={"Authorization": f"Discogs token={token}"},
    )
    mb = HttpJsonClient(MB_INTERVAL)

    done = skipped = found = missing = errors = 0
    for i, cat in enumerate(catalogs, 1):
        key = catalog_key(cat)
        existing = records.get(key)
        if (
            not force
            and existing
            and existing.get("status") in ("ok", "not_found")
            and existing.get("source") in ("discogs", "musicbrainz")
        ):
            skipped += 1
            continue

        print(f"[{i}/{len(catalogs)}] {cat} ...", end=" ", flush=True)
        tried: list[str] = []
        try:
            hit, d_tried = search_discogs(discogs, cat)
            tried.extend(d_tried)
            if hit:
                rid = hit.get("id")
                enriched = discogs.get_json(DISCOGS_RELEASE.format(rid=rid)) if rid else None
                records[key] = record_from_discogs(cat, hit, enriched, tried)
                found += 1
                print(f"ok/discogs — {records[key].get('title')}")
            else:
                mb_hit, m_tried = search_musicbrainz(mb, cat)
                tried.extend(m_tried)
                if mb_hit:
                    mbid = mb_hit.get("id")
                    q = urllib.parse.urlencode(
                        {
                            "fmt": "json",
                            "inc": "artists+labels+recordings+artist-credits",
                        }
                    )
                    enriched = mb.get_json(f"{MB_LOOKUP.format(mbid=mbid)}?{q}") if mbid else None
                    records[key] = record_from_mb(cat, mb_hit, enriched, tried)
                    found += 1
                    print(f"ok/mb — {records[key].get('title')}")
                else:
                    records[key] = empty_record(cat, "not_found", "discogs+musicbrainz", tried)
                    missing += 1
                    print("not_found")
        except Exception as e:
            records[key] = empty_record(cat, "error", "discogs+musicbrainz", tried, error=str(e))
            errors += 1
            print(f"error — {e}")

        done += 1
        if done % 10 == 0:
            rewrite_cache(CACHE_PATH, records, order)

    rewrite_cache(CACHE_PATH, records, order)
    print(
        f"Finished. looked_up={done} skipped={skipped} found={found} "
        f"not_found={missing} errors={errors} cache={CACHE_PATH}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    lookup_all(force=args.force, limit=args.limit)


if __name__ == "__main__":
    main()
