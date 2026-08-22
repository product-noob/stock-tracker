"""Extract text layer from each unique (by hash) filing PDF. Flags image-only/low-confidence PDFs per §6."""
import json
import sys
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).parent))
from companies import get

co = get(sys.argv[1] if len(sys.argv) > 1 else "tataelxsi")
DATA = Path(__file__).parent.parent / "data" / co.slug
TEXT_DIR = DATA / "extracted_text"
TEXT_DIR.mkdir(exist_ok=True)

MIN_CHARS_PER_PAGE = 30  # below this, page is almost certainly image-only/scanned


def extract_one(pdf_path: Path) -> dict:
    pages_text = []
    low_confidence_pages = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages_text.append(text)
            if len(text.strip()) < MIN_CHARS_PER_PAGE:
                low_confidence_pages += 1
        n_pages = len(pdf.pages)
    full_text = "\n\n".join(pages_text)
    return {
        "n_pages": n_pages,
        "total_chars": len(full_text),
        "low_confidence_pages": low_confidence_pages,
        "image_only": low_confidence_pages == n_pages,
        "text": full_text,
    }


def main():
    manifest = json.loads((DATA / "raw_meta" / "pdf_manifest.json").read_text())

    # collapse to one entry per unique hash (dedup already proven in Step 2)
    by_hash = {}
    for r in manifest:
        if not r.get("downloaded"):
            continue
        h = r["sha256"]
        by_hash.setdefault(h, {"records": [], "local_path": r["local_path"]})
        by_hash[h]["records"].append({"exchange": r["exchange"], "subject": r["subject"], "published_at": r["published_at"]})

    results = []
    for h, group in by_hash.items():
        pdf_path = Path(group["local_path"])
        try:
            extraction = extract_one(pdf_path)
        except Exception as e:
            extraction = {"error": str(e), "n_pages": None, "total_chars": 0, "image_only": None}

        text_path = TEXT_DIR / f"{h[:16]}.txt"
        if "text" in extraction:
            text_path.write_text(extraction["text"], encoding="utf-8")

        results.append({
            "sha256": h,
            "source_records": group["records"],
            "pdf_path": str(pdf_path),
            "text_path": str(text_path) if "text" in extraction else None,
            "n_pages": extraction.get("n_pages"),
            "total_chars": extraction.get("total_chars"),
            "low_confidence_pages": extraction.get("low_confidence_pages"),
            "image_only": extraction.get("image_only"),
            "error": extraction.get("error"),
        })
        status = "IMAGE-ONLY" if extraction.get("image_only") else ("ERROR" if extraction.get("error") else "OK")
        print(f"{status:12s} {h[:12]}  pages={extraction.get('n_pages')}  chars={extraction.get('total_chars')}  {group['records'][0]['subject']}")

    out_path = DATA / "raw_meta" / "extraction_manifest.json"
    out_path.write_text(json.dumps(results, indent=2))

    n_ok = sum(1 for r in results if not r["error"] and not r["image_only"])
    n_image = sum(1 for r in results if r["image_only"])
    n_err = sum(1 for r in results if r["error"])
    print(f"\n{len(results)} unique documents | text OK: {n_ok} | image-only: {n_image} | errors: {n_err}")


if __name__ == "__main__":
    main()
