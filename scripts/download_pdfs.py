"""Download all filing PDFs referenced in normalized_documents.json and hash them for dedup."""
import hashlib
import json
import subprocess
from pathlib import Path

DATA = Path(__file__).parent.parent / "data" / "tataelxsi"
PDF_DIR = DATA / "pdfs"
PDF_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def curl_get(url: str, referer: str, out_path: Path) -> bool:
    subprocess.run(
        ["curl", "-s", "-A", UA, "-e", referer, str(url), "-o", str(out_path)],
        check=True,
    )
    return out_path.exists() and out_path.stat().st_size > 0 and _is_pdf(out_path)


def _is_pdf(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(4) == b"%PDF"


def bse_url_variants(filename: str):
    yield f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{filename}"
    yield f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{filename}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main():
    records = json.loads((DATA / "raw_meta" / "normalized_documents.json").read_text())
    results = []
    for r in records:
        seq = r["source_seq_id"]
        out_path = PDF_DIR / f"{r['exchange']}_{seq}.pdf"
        ok = False
        if r["exchange"] == "NSE" and r["document_url"]:
            ok = curl_get(r["document_url"], "https://www.nseindia.com/", out_path)
        elif r["exchange"] == "BSE" and r["document_url"]:
            filename = r["document_url"].rsplit("/", 1)[-1]
            for candidate in bse_url_variants(filename):
                if curl_get(candidate, "https://www.bseindia.com/", out_path):
                    ok = True
                    break
        entry = {**r, "local_path": str(out_path) if ok else None, "downloaded": ok}
        if ok:
            entry["sha256"] = sha256(out_path)
            entry["bytes"] = out_path.stat().st_size
        results.append(entry)
        print(f"{'OK ' if ok else 'FAIL'} {r['exchange']:3s} {seq}")

    out = DATA / "raw_meta" / "pdf_manifest.json"
    out.write_text(json.dumps(results, indent=2))

    downloaded = [r for r in results if r["downloaded"]]
    print(f"\n{len(downloaded)}/{len(results)} PDFs downloaded -> {PDF_DIR}")

    # content-hash dedup
    by_hash = {}
    for r in downloaded:
        by_hash.setdefault(r["sha256"], []).append(r)
    dupes = {h: recs for h, recs in by_hash.items() if len(recs) > 1}
    print(f"Unique documents by content hash: {len(by_hash)} (from {len(downloaded)} downloaded)")
    print(f"Cross-exchange duplicate groups: {len(dupes)}")
    for h, recs in dupes.items():
        print(f"  {h[:12]}... -> {[ (r['exchange'], r['subject']) for r in recs ]}")


if __name__ == "__main__":
    main()
