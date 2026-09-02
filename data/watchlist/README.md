# Synthetic watchlist dataset

**This data is entirely SYNTHETIC. It is not real police data, not a real
FIR, not a real missing-person case, and does not reference any real
vehicle, person, or investigation.** Plates, case references, and dates were
invented for development and demo purposes only — to exercise
`services/match-engine` against a realistic-looking watchlist without
touching anything sensitive before a real integration exists.

## Contents

- `synthetic_watchlist.json` — the primary snapshot. Covers all four
  `WatchlistReason` values used at write time (`stolen`, `wanted`,
  `missing_person`, `blacklisted`, `suspect`), plus:
  - one `STANDARD`-format plate per reason (`GJ01AB1234`, `GJ05CD5678`, ...),
  - one `BH_SERIES` (Bharat series, portable-registration) plate
    (`23BH1234AB`),
  - one entry with an `expires_at` in the past, to exercise expiry filtering
    in `matcher.py`,
  - one deliberately `NONCONFORMING` plate (`TRAC7788` — shaped like a
    farm-vehicle/trailer plate, not a standard registration), to prove the
    matcher does not require a parseable format to index and score an entry.
- `synthetic_watchlist_supplement.csv` — a second snapshot in CSV, loaded
  alongside the JSON file, demonstrating `Watchlist.load_dir`'s support for
  both formats from the same directory.

## Loading

`Watchlist.load_dir(Path("data/watchlist"))` loads every `.json` and `.csv`
file directly inside this directory. Every plate is normalised through
`prahari_common.plates.normalise_plate` on load — the same grammar the
inference service applies to OCR output — so a plate written here with
different spacing or casing than what a camera reports still lands in the
same skeleton bucket.

## Adding a real snapshot

When a real watchlist feed is integrated, it should land in this same
directory in the same shape (or via a loader added to `watchlist.py`) —
but as a separate, access-controlled artifact, never committed to this
repository. This synthetic set should stay here as the fixture used by
`services/match-engine/tests/`.
