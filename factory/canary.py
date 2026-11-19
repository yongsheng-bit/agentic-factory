"""Golden canary: run the merged parser against David's real receipts.

Every test in petro-tools runs on synthetic PDFs the agent generated from
its own understanding of the receipt format - which is exactly the
understanding that has been wrong before (ticket 1 burned three attempts
on a fixture assumption). The ground truth is not synthetic: it is the
hand-verified scan.csv David keeps next to the real Petro-Canada PDFs.
This script parses every real receipt and diffs the rows against that
CSV, so "the suite is green" and "the tool works on my station's paper"
become separately checkable claims.

Privacy contract: real station data is READ here and never written
anywhere - the output is counts and dates, never liter values or
receipt text. It lives in agentic-factory, not petro-tools, so no real
data path ever enters the product repository. Run it with the venv that
has pymupdf:

    python canary.py --repo C:/Users/David/petro-tools \
        --data C:/Users/David/Documents/petro \
        --truth C:/Users/David/Documents/petro/scan.csv
"""

import argparse
import csv
import glob
import os
import sys


def load_truth(path):
    truth = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            key = (row["date"].strip(), row["dispenser"].strip(),
                   row["grade"].strip())
            liters = (row.get("liters") or "").strip()
            if liters:
                truth[key] = float(liters)
    return truth


def parse_pdf(fitz, parse_receipt, path):
    warn = []
    with fitz.open(path) as doc:
        text = doc[0].get_text()
    _, rows = parse_receipt(text, warn=warn.append)
    return rows, warn


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="petro-tools checkout")
    ap.add_argument("--data", required=True, help="folder of Receipt_*.pdf")
    ap.add_argument("--truth", required=True, help="scan.csv ground truth")
    ap.add_argument("--tolerance", type=float, default=0.005)
    args = ap.parse_args(argv)

    sys.path.insert(0, args.repo)
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            print("canary needs pymupdf - run it with the petro-tools-env python")
            return 2
    from petro.parse import parse_receipt

    truth = load_truth(args.truth)
    truth_dates = sorted({d for d, _, _ in truth})
    print(f"truth: {len(truth)} rows over {len(truth_dates)} dates")

    pdfs = sorted(glob.glob(os.path.join(args.data, "Receipt_*.pdf")))
    if not pdfs:
        print(f"no Receipt_*.pdf under {args.data}")
        return 2

    parsed = {}
    empty_text = []
    for path in pdfs:
        rows, warn = parse_pdf(fitz, parse_receipt, path)
        if not rows:
            empty_text.append(os.path.basename(path))
        for row in rows:
            key = (row["date"], row["dispenser"], row["grade"])
            parsed.setdefault(key, float(row["liters"]))

    files = len(pdfs)
    dates = sorted({d for d, _, _ in parsed})
    print(f"parsed: {len(parsed)} rows over {len(dates)} dates "
          f"from {files} PDFs")
    for name in empty_text:
        print(f"NO ROWS: {name} (empty text layer or parser miss)")

    missing = sorted(set(truth) - set(parsed))
    extra = sorted(set(parsed) - set(truth))
    wrong = sorted(k for k in set(parsed) & set(truth)
                   if abs(parsed[k] - truth[k]) > args.tolerance)

    for key in missing[:10]:
        print(f"MISSING {key[0]} {key[1]} {key[2]} (in CSV, parser missed)")
    for key in extra[:10]:
        print(f"EXTRA   {key[0]} {key[1]} {key[2]} (parser invented it)")
    for key in wrong[:10]:
        print(f"WRONG   {key[0]} {key[1]} {key[2]} (liters differ past "
              f"tolerance)")

    ok = not missing and not extra and not wrong and not empty_text
    print(f"canary: {'PASS' if ok else 'FAIL'} - "
          f"{len(parsed) - len(wrong)}/{len(truth)} truth rows reproduced "
          f"exactly, {len(missing)} missing, {len(extra)} extra, "
          f"{len(wrong)} wrong, {len(empty_text)} files with no rows")
    if len(empty_text) == files:
        print("canary: NO receipt had a text layer - the OCR chain "
              "(scan orchestration + rapidocr adapter) is the blocker, "
              "not the text parser")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
