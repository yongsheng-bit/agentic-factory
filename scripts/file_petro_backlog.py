"""File the initial petro-tools backlog as factory-labelled GitHub issues."""

import subprocess

GH = r"C:\Users\David\AppData\Local\Programs\gh\bin\gh.exe"
REPO = "yongsheng-bit/petro-tools"

COMMON = """

---
Rules for this repo:
- Tests are stdlib `unittest` under `tests/`, runnable with `PYTHONPATH=. python -m unittest discover -s tests`.
- Fixtures must be SYNTHETIC (build PDFs with pymupdf, workbooks with openpyxl, in-test). Real station data never enters this repository.
- No `#` comments in Python files; rationale goes in docstrings (CI enforces).
- Keep the diff to this ticket only; do not refactor unrelated modules.
"""

TICKETS = [
("csvio: CSV schema, append, and dedup sidecars",
"""Implement `petro/csvio.py` for the cumulative scan CSV.

- `read_rows(path)` / `append_rows(path, rows)`: long format with header `date,dispenser,grade,liters,dollars` (module constant `COLUMNS`). File is UTF-8 **with BOM** so Excel opens headers intact. Missing file is created with the header; a file whose header differs from `COLUMNS` raises `SchemaError` and is never written.
- `rows` are dicts with the five keys; liters format to 2 decimals, empty string when absent.
- Sidecars next to the CSV: `<csv>.done.txt` and `<csv>.skipped.txt`, one filename per line. `mark_done(path, name)`, `is_done(path, name)`, `mark_skipped(path, name)`, `is_skipped(path, name)`.

Acceptance:
- Round-trip test: append then read returns the same values.
- Test asserts the file starts with the UTF-8 BOM.
- Test: header drift (e.g. extra column) raises `SchemaError` and leaves the file untouched.
- Tests for sidecar mark/check round-trip.
""" + COMMON),

("parse: grade normalization for OCR damage",
"""Implement `normalize_grade(text) -> str | None` in `petro/parse.py`.

Repairs known OCR damage on Petro-Canada grade labels and returns one of `REGULAR, PLUS, SUPREME, DIESEL, ULTRA94` (module constant `GRADES`) or None:
- `ULTRA 94`, `ULTRA94`, `ULTRA.94`, `UTRA94` all normalize to `ULTRA94` (stray dot/space tolerated).
- `REGLLAR`, `REGLAR` normalize to `REGULAR`.
- Leading `*` (receipt marker) and surrounding whitespace are stripped.
- Case-insensitive.

Acceptance:
- Table-driven test covering every variant above plus a negative case (`COFFEE` -> None).
""" + COMMON),

("parse: text-layer PUMP VOLUMES receipt parser",
"""Implement `parse_receipt(text) -> (header, rows)` in `petro/parse.py`, parsing the embedded text layer of a PUMP VOLUMES / SELF SERVE receipt.

- `header` is a dict with at least `{"date": "YYYY-MM-DD"}` from the `DATE: YYYY-MM-DD` line; missing date yields empty string plus a warning through a `warn(msg)` callback parameter.
- Dispenser sections start at `DISPENSER <n>` headers. The regex must tolerate misreads: `DISP\\w*NSER`, `DISPNSER`, `SPENSER` followed by the pump number (this prevents silently dropping a pump's rows).
- Inside a section, a grade line yields one row: liters is the **first 2-decimal number** on the grade line (robust to `PLUS 1 412226.05` where a volume marker `L` misreads as `1`). Dollar lines (starting with `$`) are ignored.
- A `GRAND TOTALS` section (tolerate `GRANDTOTALS`, `GRANU TOTALS`, and split `GRAND`/`TOTALS` lines) ends pump parsing; rows after it are NOT pump rows.
- Duplicate guard: a second value for the same `(dispenser, grade)` cell is dropped with a warning (first value wins).
- `expected_row_count(dispensers)` helper: 4 grades x pumps without diesel + 5 grades x D5/D6 = 26 for D1..D6 (DIESEL only on D5, D6). `parse_receipt` warns when the count differs.
- Rows are dicts: `{"date", "dispenser", "grade", "liters", "dollars": ""}`.

Acceptance:
- Fixture: a synthetic receipt text (build it as a string constant in the test) with all 6 dispensers parses to exactly 26 rows.
- Tests: misread dispenser header still yields that pump's rows; totals-section bleed does not produce duplicate D6 rows; duplicate guard warns and keeps first; missing DATE warns with empty date.
""" + COMMON),

("reconcile: pump cells must sum to printed GRAND TOTALS",
"""Implement `petro/reconcile.py`.

- `reconcile(rows, totals, tolerance=TOLERANCE_L) -> list[str]`: for each grade, sum the pump cells from `rows` (list of row dicts as produced by the parser) and compare against the printed total for that grade in `totals` (dict grade -> float). Return human-readable mismatch messages; empty list means every grade ties.
- Printed GRAND TOTALS come in two formats: daily-sales (compare as-is) or raw cumulative-meter sums printed x100 (divide by 100). Try BOTH interpretations; a grade passes if either matches within tolerance. A uniform miss across all grades means the wrong interpretation, not bad cells.
- `parse_totals(text) -> dict`: pull the printed totals from the GRAND TOTALS section of receipt text (grade label + number).

Acceptance:
- Test: matching set passes under the raw interpretation.
- Test: x100-printed totals pass via the divide-by-100 interpretation.
- Test: one bad cell (off by more than 1.0 L) produces exactly one mismatch message naming that grade.
""" + COMMON),

("scan: orchestrate folder -> CSV with injectable OCR engine",
"""Implement `run(args)` in `petro/scan.py` (CLI already wires `--folder --csv --pattern --dpi`).

- For each file matching `--pattern` in `--folder` (default `*.pdf`), skip if in the done/skipped sidecars (use `petro.csvio`).
- Extract page 1 text with pymupdf (`page.get_text()`). When the text layer is empty, call an injected OCR engine: `run(args, engine=None, warn=print)` where an engine is a callable `engine(image_bytes) -> list[(x0,y0,x1,y1,text)]` boxes; with no engine and no text layer, record the file in the skipped sidecar with a warning. (The rapidocr adapter in `petro/ocr.py` may stay unimplemented; the seam and the fake-engine test are what matter.)
- Parse with `petro.parse.parse_receipt` (text) — for OCR boxes, a coordinate-grid parser may be a follow-up ticket; for now convert boxes to text lines by y-clustering and feed `parse_receipt`.
- Never append a partially-reconciled receipt: parse, then check the 26-row expectation; on failure warn and skip the file (sidecar `skipped`), do not append.
- Append rows via `csvio.append_rows`, mark done. Print a per-file summary line and a final `N new, M skipped` line. Exit 0 even when files were skipped; exit 1 only on fatal errors (e.g. schema drift).

Acceptance:
- Integration test: build two synthetic text-layer PDFs in a temp dir with pymupdf (`insert_text` lines), run `scan.run`, assert the CSV holds 52 rows with BOM and the sidecar lists both files; re-run yields `0 new`.
- Test: a corrupt/empty-text PDF with no engine lands in skipped and the CSV is untouched.
""" + COMMON),

("scan: scan.xlsx mirror derived from the CSV",
"""Implement `write_xlsx(csv_path, xlsx_path)` in `petro/scan.py` and call it at the end of `run()` when the CSV gained rows or the xlsx is missing.

- Mirror the CSV into a single sheet named `scan` with openpyxl, column widths sized to the header/content (e.g. 12 for date/dispenser/grade, 14 for liters/dollars).
- If the xlsx is open in Excel the write raises `PermissionError` — catch it, print a note that the CSV still holds the data, and continue (exit code unaffected).
- The xlsx is a derived view: always rewritten from the CSV, never merged.

Acceptance:
- Test: after a scan run (reuse the synthetic-PDF fixture pattern), `scan.xlsx` exists, sheet `scan` has header + N rows matching the CSV.
- Test: deleting the xlsx and re-running rebuilds it with 0 new PDFs.
- Test: a target path that raises PermissionError (monkeypatch `openpyxl.Workbook.save`) prints the note and does not crash.
""" + COMMON),

("dips: resolve the date-block chain in Dips.xlsx",
"""Implement `resolve_blocks(workbook) -> dict[date, int]` in `petro/dips.py` (date -> header row).

Sheet1 layout: each date block starts at a header row where col B holds the day (a literal int at month starts, otherwise a formula `=B<row>+1` chaining from the previous block) and col C holds a month-year string like `July 2026` / `Sept 2026` at month starts. The row below the header holds the product labels (`Pump Reading | MAXIMUM | ...`).

- Walk the sheet top to bottom; a header row is one whose col B is an int (literal) or whose col C matches a month name (first three letters, case-insensitive: `Sept`/`Sep`/`September` all resolve).
- Resolve `=B<row>+1` formulas arithmetically from the previous block's date; `=#REF!+1` or unresolvable formulas above the first anchor are skipped silently (broken chain).
- Return `{datetime.date: header_row}` for every resolvable block.
- `find_block(blocks, date)` raises `KeyError` naming the closest existing block date when missing.

Acceptance:
- Test fixture: build a synthetic workbook with openpyxl — two month blocks (literal anchors `25`/`July 2026` and `1`/`Sept 2026`) chained with `=B<row>+1` formulas — and assert every date maps to the right row.
- Test: a broken chain (formula referencing a non-numeric cell) skips only the unresolvable rows.
- Test: `find_block` on a missing date raises with the nearest date in the message.
""" + COMMON),

("dips: fill one date block from the scan CSV",
"""Implement `run(args)` in `petro/dips.py` (CLI wires `--dips --csv --date --overwrite --no-backup`).

- Load the CSV rows for `--date` (via `petro.csvio`), resolve the block with `resolve_blocks`.
- Column mapping is the module constant `GRADE_TO_COLUMN` (REGULAR->B MAXIMUM, PLUS->C MAX PLUS, SUPREME->D MAXIMUM SUPREME, DIESEL->E, ULTRA94->F). Pump number N from the dispenser id (`D1`..`D6`) maps to the Nth row below the product-label row.
- DIESEL rows only exist for D5/D6; pumps 1-4 E-cells stay blank (never zeros).
- Non-empty target cells are skipped with a warning unless `--overwrite`.
- Copy the workbook to `Dips_backup.xlsx` next to it before saving unless `--no-backup`.
- Print the filled block (rows, columns B-F) for verification. Exit 0; exit 1 if the date block or CSV rows are missing.
- Dates without CSV rows are never filled — that is handled by simply not being invoked, but `run` must refuse (exit 1, no write) rather than blanking a block.

Acceptance:
- Test: synthetic dips workbook (reuse the chain fixture) + synthetic CSV; after fill, the 26 cells (22 B-D/F + 4 E across D5/D6... precisely: 4 grades x 6 pumps + DIESEL x 2 = 26) hold the CSV values as floats, E1-E4 blank.
- Test: re-run without `--overwrite` changes nothing and warns; with `--overwrite` replaces.
- Test: backup file created by default, absent with `--no-backup`.
""" + COMMON),

("shifts: parse the EOD invoice text",
"""Implement `parse_invoice(text) -> dict` in `petro/shifts.py`.

Input is the OCR'd/text-layer text of a Petro-Canada POS SHIFTREPORT invoice. Return `{"shift": <int>, "fields": {label: float}}` with exactly the 8 `INVOICE_FIELDS`, tolerating OCR variants:
- `Shift Number 13497` (also `Shift Number:13497`).
- `Fuel_sales` / `Fuel Sales`; `Item Sales`; `Total Sales`; `TotalPOS` / `Total POS`; `Petro-PointsDollarReward` / `Petro-Points Dollar Reward`; `Pump Overruns`; `Payouts`; `Safedrops 1 935.00` -> Cash Drops = **935.00** (the second number, the amount, never the count).
- `Payouts` must match the label exactly (normalized) so it can never hit `Lottery Payouts`.
- Missing field -> `None` value in `fields` (parse is tolerant; the filler decides).

Acceptance:
- Table-driven test with a synthetic invoice text containing OCR-variant labels asserts all 8 values plus the shift number.
- Test: an invoice with a `Lottery Payouts` line does not contaminate `Payouts`.
- Test: `Safedrops 2 1200.50` yields 1200.50.
""" + COMMON),

("shifts: resolve shift columns across sheets",
"""Implement in `petro/shifts.py`:

- `resolve_shift_columns(worksheet) -> dict[int, str]`: shift numbers live in row 2 — a literal anchor (e.g. `13497` in D2) then chains `=D2+1`, `=E2+1`, ... Resolve left-to-right; non-numeric or unresolvable formulas are skipped. Returns shift number -> column letter.
- `resolve_days(worksheet) -> dict[str, str]`: same pattern for row 3 (day-of-month chain), column letter -> day value.
- `find_shift(workbook, shift, day=None) -> (sheet_name, column)`: search EVERY sheet's resolved row-2 chain (sheet names are unreliable and must never be parsed). If several sheets contain the shift, `day` (day-of-month from the invoice date) disambiguates via row 3; without a day hint, a multi-match raises.
- `find_label_row(worksheet, label) -> int`: metric rows are found by their column-A label, normalized (lowercase, strip spaces/underscores/colons) — never hardcoded rows. Normalized `payouts` must match the row labelled `Payouts` and never `Lottery Payouts`.

Acceptance:
- Fixture: synthetic workbook, two month sheets, row-2 literal + `=X+1` chains; assert every shift maps to the right sheet+column and the duplicate-shift case resolves via the day row.
- Test: `find_label_row` matches `Fuel Sales` from a cell reading `fuel_sales:` and picks `Payouts` over `Lottery Payouts`.
""" + COMMON),

("shifts: fill the shift column from the invoice",
"""Implement `run(args)` in `petro/shifts.py` (CLI wires `--invoice --report --overwrite --no-backup`).

- Read the invoice: text layer via pymupdf; if empty, error out with a clear message (OCR adapter is a later ticket). Parse with `parse_invoice`.
- Locate the column with `find_shift` (day hint from the invoice date when present in the text).
- For each of the 8 fields, `find_label_row` and write the value into that row of the shift column. `None` fields are skipped with a warning.
- Non-empty target cells skipped with a warning unless `--overwrite`.
- Backup to `Shift_Report_backup.xlsx` next to the report unless `--no-backup`.
- Print the parsed invoice values first, then the filled column (row label = value), then save. Exit 0; exit 1 when the shift column or invoice parse fails.

Acceptance:
- Test: synthetic invoice text PDF + synthetic report workbook; after fill, the 8 cells hold the invoice values and the printed output lists them.
- Test: re-run without `--overwrite` warns and changes nothing.
- Test: unknown shift number exits 1 without touching the workbook.
""" + COMMON),

("audit: verify Dips.xlsx against the scan CSV",
"""Implement `run(args)` in `petro/audit.py` (CLI wires `--dips --csv --from --to`).

- Resolve blocks with the SAME `petro.dips.resolve_blocks` the filler uses (the two must never disagree).
- For every date in `[from, to]`: compare each filled B-F cell against the CSV rows (float compare, tolerance 0.005). Report per-cell mismatches as `YYYY-MM-DD D<n> <grade>: workbook=<x> csv=<y>`.
- Dates with no CSV rows must have an EMPTY block; any value there is reported as `unexpected data on a no-receipt date`.
- Dates with CSV rows must have all 26 cells (DIESEL only D5/D6); missing cells are reported.
- Exit 0 when clean, exit 1 with the mismatch list otherwise.

Acceptance:
- Test: filled fixture passes clean (exit 0).
- Test: one perturbed cell produces exactly one mismatch line and exit 1.
- Test: a no-receipt date with a stray value is flagged.
""" + COMMON),

("shifts: rapidocr adapter for scanned invoices",
"""Implement the OCR path used when a PDF has no text layer.

- `petro/ocr.py`: `rapidocr_engine()` returns a callable `engine(image_bytes) -> list[(x0,y0,x1,y1,text)]` wrapping `rapidocr_onnxruntime.RapidOCR` (rendering to image bytes stays the caller's job via pymupdf). Import of rapidocr must be lazy so the package works without the `ocr` extra.
- `petro/scan.py` and `petro/shifts.py`: when the text layer is empty, render page 1 at `--dpi` with pymupdf, run the engine, and feed the boxes through the existing y-clustering path.
- `run(args)` functions accept an `engine=` parameter (default: lazily build the rapidocr engine when a page needs it).

Acceptance:
- Test with a FAKE engine (returns canned boxes for a synthetic scanned PDF): scan and shift both complete end-to-end without rapidocr installed.
- Test: importing `petro.ocr` and calling `rapidocr_engine` is deferred — module import must not import rapidocr (assert via `sys.modules`).
""" + COMMON),

("docs: README usage matches the shipped CLI",
"""Update `README.md` so every documented flag exists in `petro.cli.build_parser()` and every subcommand is shown with a realistic invocation (temp-folder examples only, no real paths).

- Add a short `## How it fits together` section: scan -> scan.csv/scan.xlsx -> dips fill -> audit; invoice -> shift fill.
- Document the exit-code contract (0 ok, 1 reported problem, 2 bad usage) and the sidecar files.
- Keep the existing rules section; correct anything that drifted.

Acceptance:
- A test parses README fenced `petro ...` command lines and asserts each one's subcommand and flags exist in the argparse parser (walk `build_parser()` subparsers).
""" + COMMON),
]

for title, body in TICKETS:
    p = subprocess.run(
        [GH, "issue", "create", "-R", REPO, "-t", title, "-b", body,
         "-l", "factory"],
        capture_output=True, text=True)
    print(title, "->", p.stdout.strip() or p.stderr.strip())
