# Collection barcode / catalog lookup

Tools to identify flute-featured discs from Kim’s dad’s collection.

- **CDs:** barcodes in this folder (`lookup_barcodes.py`)
- **LPs:** catalog numbers from `JWG_LPs.numbers` in [`lp/`](lp/) (`lookup_lps.py`)

## Files

| File | Role |
|------|------|
| `barcodes.raw.txt` | Original paste (may include duplicates / odd codes) |
| `barcodes.txt` | Unique normalized UPC/EAN/ISBN codes |
| `non_upc.txt` | Codes that are not barcodes (`D162571`, etc.) |
| `metadata.jsonl` | One JSON record per code — **durable cache** |
| `filter_flute.yml` | Local filter rules (edit freely) |
| `collection_ok.csv` | All identified (`status=ok`) rows, unfiltered |
| `unresolved.txt` | Leftovers after MusicBrainz + Discogs (no third pass) |
| `not_found.txt` | Barcode-only unresolved list |
| `flute_featured.*` | Confident flute-featured hits |
| `flute_candidates.csv` | Keyword hits for a quick glance |

## Re-run lookup (network)

```bash
python3 collection/lookup_barcodes.py
```

Resume-safe: skips barcodes already `ok` or `not_found`. Use `--force` to refetch. MusicBrainz is rate-limited (~1 req/s).

Discogs fallback for MusicBrainz misses (token in gitignored `collection/.discogs_token` or `DISCOGS_TOKEN`):

```bash
python3 collection/lookup_barcodes.py --discogs-fallback
```

Normalize only:

```bash
python3 collection/lookup_barcodes.py --normalize-only
```

## Export unfiltered OK metadata (no network)

```bash
python3 collection/export_ok_csv.py
```

Writes `collection_ok.csv` — every `status=ok` row with title/artists/tracks/credits/etc. Do this before filtering if you want the full identified list.

## Re-run filter (no network)

```bash
python3 collection/filter_collection.py
```

Edit `filter_flute.yml` then re-run the filter — no need to look barcodes up again.
