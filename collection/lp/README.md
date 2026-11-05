# LP collection (JWG)

Extracted from `~/Documents/JWG_LPs.numbers` (Label & Catalog#, Front/Back photos).

Photos stay in the Numbers file for visual reference (`numbers-parser` does not expose them as cell values). Lookup uses **catalog numbers**.

## Pipeline

```bash
# 1. Re-extract catalogs from Numbers (optional)
python3 -c "..."  # or re-run extract steps documented in extract_summary.json

# 2. Lookup (Discogs catno → MusicBrainz catno fallback)
python3 collection/lp/lookup_lps.py

# 3. Unfiltered OK metadata
python3 collection/export_ok_csv.py \
  --cache collection/lp/metadata.jsonl \
  --out collection/lp/collection_ok.csv

# 4. Flute filter (same rules as CDs)
python3 collection/filter_collection.py \
  --cache collection/lp/metadata.jsonl \
  --config collection/filter_flute.yml \
  --outdir collection/lp
```

Unresolved catalogs are written to `unresolved.txt` after lookup.
