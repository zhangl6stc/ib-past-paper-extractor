# IB Past Paper Question & Markscheme Extractor

Extracts individual questions (and their markschemes) from IB past-paper PDFs:
downloads PDFs from an unofficial mirror, slices every question's page image and
the matching markscheme into JPEG slices in RAM, and writes a CSV describing
each question row (subject, year, session, paper, page, question number, image
path, markscheme link/image).

No PDF is ever written to disk — all slicing happens in memory (pdftoppm's own
temp files are handled by pdf2image).

## How it works (short version)

- A **list file** enumerates every QP + markscheme PDF on the mirror
  (see "Getting the list of papers").
- Question papers and markschemes are paired from the same directory by stem.
- QP page slicing anchors on `N.` + `[Maximum mark: X]` headers; where the
  headers are missing it falls back to sequence detection (bare `N.` at the
  left margin, strictly increasing). `(Question N continued)` pages are
  stitched vertically onto their question. Scanned PDFs (no text layer) fall
  back to a simple two-halves split.
- Markscheme slicing is gated on `Section A/B` headers and its own sequence
  rules, with fallbacks (`full_ms` stacked image when nothing is found).
- `*_case_study` booklets are never sliced (their numbered reference lists
  look like question sequences).

## Requirements

- Python 3.8+
- Poppler (`pdftoppm`/`pdfinfo` binaries) on your PATH — Windows users can
  grab the latest from
  https://github.com/oschwartz10612/poppler-windows/releases and add the
  extracted `Library\bin` folder to PATH (or set `POPPLER_BIN` to it — the
  bundled `scripts/run_shard.cmd` runner honors that variable).
- `pip install -r requirements.txt`
- Supabase (`supabase` package) is optional: only `--storage supabase` uses it.

## Getting the list of papers

The mirror is an open directory whose root maps to
`https://ibdocs.re/p/IB PAST PAPERS - YEAR/`. Refresh the local listing with:

```
python scripts/dump_ibdocs.py --out lists/by_year_file_list.txt
```

(`scripts/dump_listing.py` is an alternative recursive dumper for a
Cloudflare-gated mirror.) Each line looks like:

```
D:\International Baccalaureate Documents\IB PAST PAPERS - YEAR\2010 Examination Session\May 2010 Examination Session\Group 4 - Sciences\Physics_paper_1_TZ2_SL_May2010.pdf
```

The `D:\...` prefix is the mirror's own root marker; only the part after
`IB PAST PAPERS - YEAR` is used to build download URLs. You can also write a
list file by hand — any path containing the root marker and ending in `.pdf`
works.

## Usage

All scripts live in `scripts/` and resolve their own data directories
(`lists/`, `output/`, ...) relative to this repository root, so they can be
invoked from any working directory.

### Single run (offline, images to disk)

```
python scripts/extract_questions.py --file lists/by_year_file_list.txt --csv-out output/questions.csv
```

- Images are saved under `extracted_samples/` (override with `--local-dir`).
- `--subject Mathematics_AAHL` restricts to one subject; `--limit 1` runs the
  first paper only (sanity check).
- `--csv-out` writes `output/questions.csv`. Note that without an explicit
  `--local-dir`, a plain run writes to the default `extracted_samples/`.

### Single run with Supabase Storage

Images can be uploaded to a Supabase public bucket instead of disk, and the
CSV rows then carry public `image_url`s:

```
set SUPABASE_URL=https://<project>.supabase.co
set SUPABASE_KEY=<anon or service-role key>
python scripts/extract_questions.py --file lists/by_year_file_list.txt --csv-out output/questions.csv --storage supabase --manifest manifests/manifest.jsonl
```

- `--manifest` resumes: image rows are reused by storage path, and
  `paper_done` markers skip finished papers.
- The bucket name defaults to `past-papers` and objects go under
  `agent-uploads/<subject>/<year>/...`; both are constants at the top of
  `extract_questions.py`.

### Parallel runs (shards)

Split the master list into per-subject/year-range lists, then run up to five
shards at a time (each shard gets its own CSV):

```
python scripts/prep_shards.py
powershell -File scripts/scheduler.ps1
```

`prep_shards.py` writes `lists/list_<shard>.txt`; `scripts/run_shard.cmd
<shard>` runs one shard (set `STORAGE=supabase` to upload instead of saving
locally); `scheduler.ps1` runs them with at most 5 concurrent processes.
Shard CSVs can be combined with a header-skipping concatenation once all
shards finish.

> Note: `scripts/fix_aahl_lists.py` is a dataset-specific helper that splits
> the Mathematics AAHL shard at the 2023/2024 syllabus change
> (`list_aahl.txt` ≤ 2023, `list_aahl_b.txt` ≥ 2024). Only needed for that
> subject layout.

## Subject filtering

By default every paper in the list is processed. `extract_questions.py`'s
classifier knows `Computer_Science`, `Economics_HL`, `Mathematics_HL`,
`Mathematics_AAHL` and `Physics_SL` folder conventions, and the subject
folders move between "Group 4 - Sciences", "Experimental sciences", etc.
across years — URLs are always taken from the list file, never guessed.

## Disclaimer

This project is **not affiliated with the International Baccalaureate
Organization**. Past papers and markschemes are © the IBO and are provided by
unofficial mirrors; this tool is for personal study and educational use only.
If you are a rights holder and want material removed, open an issue and it
will be taken down.

## License

MIT — see [LICENSE](LICENSE).
