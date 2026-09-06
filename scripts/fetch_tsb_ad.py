"""Fetch the TSB-AD-M multivariate benchmark corpus into Datasets/.

TSB-AD is the labelled half of the data plan: the live solar feed proves the pipeline runs
on genuinely unseen data, and this corpus is where precision and recall can actually be
computed. Roughly 515 MB compressed, 1.6 GB extracted, so it is git-ignored and fetched on
demand rather than vendored.

Resumable: a partial download is continued with a Range request rather than restarted.

    python scripts/fetch_tsb_ad.py
    python scripts/fetch_tsb_ad.py --check     # verify an existing copy, download nothing
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

TSB_AD_M_URL = "https://www.thedatum.org/datasets/TSB-AD-M.zip"
DATASETS_DIR = Path(__file__).resolve().parents[1] / "Datasets"
ARCHIVE = DATASETS_DIR / "TSB-AD-M.zip"
EXTRACT_DIR = DATASETS_DIR / "TSB-AD-M"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def download(url: str, dest: Path, chunk: int = 1 << 20) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = dest.stat().st_size if dest.exists() else 0

    request = urllib.request.Request(url, headers={"User-Agent": "vigil-benchmark-fetch/0.1"})
    if existing:
        request.add_header("Range", f"bytes={existing}-")
        print(f"resuming from {_human(existing)}", flush=True)

    with urllib.request.urlopen(request, timeout=60) as response:
        resuming = response.status == 206
        if existing and not resuming:
            print("server ignored the range request; restarting the download", flush=True)
            existing = 0
        declared = response.headers.get("Content-Length")
        total = (int(declared) + existing) if declared else None
        mode = "ab" if resuming else "wb"
        got = existing
        started = time.perf_counter()
        last_line = started
        with dest.open(mode) as fh:
            while True:
                block = response.read(chunk)
                if not block:
                    break
                fh.write(block)
                got += len(block)
                now = time.perf_counter()
                if now - last_line >= 2.0:
                    rate = (got - existing) / max(now - started, 1e-9)
                    pct = f" ({100.0 * got / total:.1f}%)" if total else ""
                    print(f"  {_human(got)}{pct} at {_human(rate)}/s", flush=True)
                    last_line = now
    print(f"downloaded {_human(dest.stat().st_size)} -> {dest}", flush=True)
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def extract(archive: Path, into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        members = zf.namelist()
        print(f"extracting {len(members):,} entries...", flush=True)
        zf.extractall(into)
    return into


def summarise(root: Path) -> None:
    csvs = sorted(root.rglob("*.csv"))
    total = sum(p.stat().st_size for p in csvs)
    print(f"\n{len(csvs):,} CSV series, {_human(total)} on disk under {root}")
    for p in csvs[:5]:
        print(f"  {p.relative_to(root)}  ({_human(p.stat().st_size)})")
    if len(csvs) > 5:
        print(f"  ... and {len(csvs) - 5:,} more")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report on the local copy and exit")
    ap.add_argument(
        "--keep-archive", action="store_true", help="do not delete the zip after extract"
    )
    args = ap.parse_args(argv)

    if args.check:
        if not EXTRACT_DIR.exists():
            print(f"not present: {EXTRACT_DIR}", file=sys.stderr)
            return 1
        summarise(EXTRACT_DIR)
        return 0

    if EXTRACT_DIR.exists() and any(EXTRACT_DIR.rglob("*.csv")):
        print(f"already extracted at {EXTRACT_DIR}")
        summarise(EXTRACT_DIR)
        return 0

    free = shutil.disk_usage(DATASETS_DIR.parent).free
    if free < 3 * 1024**3:
        print(
            f"only {_human(free)} free; need roughly 2.5 GB for archive + extract",
            file=sys.stderr,
        )
        return 1

    download(TSB_AD_M_URL, ARCHIVE)
    print(f"sha256 {sha256(ARCHIVE)}", flush=True)
    extract(ARCHIVE, DATASETS_DIR)
    if not args.keep_archive:
        ARCHIVE.unlink(missing_ok=True)
    root = EXTRACT_DIR if EXTRACT_DIR.exists() else DATASETS_DIR
    summarise(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
