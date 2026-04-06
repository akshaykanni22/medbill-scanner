# Data source: CMS.gov HCPCS Level II and Physician Fee Schedule
# Public domain — U.S. government works are not subject to copyright
# Source: https://www.cms.gov
"""
scripts/download_cms_data.py
============================================================
PURPOSE:
    Downloads CMS public data (HCPCS codes + Medicare fee
    schedule), cleans it, and saves to data/processed/ so
    the RAG ingest step can load it into ChromaDB.

RUN THIS ONCE before starting the app:
    docker-compose run backend python /app/scripts/download_cms_data.py

WHY WE USE CMS DATA (not AMA CPT):
    - AMA CPT is copyrighted — using it without a license is illegal
    - CMS HCPCS Level II is public domain (government work)
    - CMS RVU file gives us Medicare reimbursement rates
      which we use as a "fair price" benchmark
    - Every code on a real patient bill maps to HCPCS

URL DISCOVERY STRATEGY:
    CMS does not publish a stable, year-independent URL for these files.
    Rather than hardcode a year that will break annually, we probe
    candidate URLs in descending year order at runtime and use the
    first one that returns HTTP 200.

    RVU:   https://www.cms.gov/files/zip/rvu{YY}a.zip
           (confirmed working for 2022–2026; CMS redirects to the
           dated release file automatically)

    HCPCS: https://www.cms.gov/files/zip/{YYYY}-alpha-numeric-hcpcs-file.zip
           (CMS naming convention observed across releases)

    Both patterns encode only the 2-digit or 4-digit year — nothing
    else — so the script self-updates every January without code changes.

SECURITY NOTES:
    - We download only from cms.gov (official government domain)
    - We verify the downloaded file is a valid ZIP before extracting
    - We extract only specific known filenames via anchored regex, never wildcard
    - No patient data is involved in this script
============================================================
"""

import io
import logging
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd

# ---- Logging setup ----
# WHY: Print statements vanish in Docker logs. Proper logging
# includes timestamps and severity, making debugging much easier.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---- Paths ----
# Data lives on the Samsung SSD, not inside the repo or container image.
# WHY: Keeps downloaded CMS files (can be large) off the Mac system drive.
#      The SSD path is also mounted into the backend container (read-only)
#      via docker-compose.yml so the app can access processed CSVs at runtime.
#
# The MEDBILL_DATA_DIR env var lets you override this for CI or other machines.
# Default: /Volumes/Sam-mini-extra/projects/medbill-scanner/data
SSD_DATA_DIR = Path(
    os.getenv(
        "MEDBILL_DATA_DIR",
        "/Volumes/Sam-mini-extra/projects/medbill-scanner/data",
    )
)
RAW_DIR = SSD_DATA_DIR / "raw"
PROCESSED_DIR = SSD_DATA_DIR / "processed"

RAW_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

log.info(f"Data directory: {SSD_DATA_DIR}")
log.info(f"Raw dir: {RAW_DIR}")
log.info(f"Processed dir: {PROCESSED_DIR}")

# ---- Constants ----
CMS_BASE = "https://www.cms.gov/files/zip"

# How many prior years to fall back through if the current year has no file yet.
# CMS typically publishes the new year's file in late November/December.
# Probing 3 years back covers any gap.
YEAR_LOOKBACK = 3


# ---- URL discovery ----

def _probe_url(url: str, client: httpx.Client) -> bool:
    """
    Return True if the URL responds with HTTP 200.

    WHY HEAD not GET: avoids downloading multi-MB files just to check existence.
    WHY follow_redirects: CMS shortname URLs (e.g. rvu26a.zip) 301-redirect to
    the dated release file; we need to follow that to get the real 200.
    """
    try:
        resp = client.head(url, follow_redirects=True)
        return resp.status_code == 200
    except httpx.RequestError:
        return False


def _discover_rvu_url(client: httpx.Client) -> str:
    """
    Find the current RVU ZIP URL by probing rvu{YY}a.zip in descending year order.

    WHY THIS PATTERN:
        CMS has used rvu{YY}a.zip as a stable shortname since at least 2022.
        The shortname 301-redirects to the actual dated release file, so we
        never need to know the full filename — CMS manages the redirect.

    SECURITY: we only probe cms.gov and only accept HTTP 200 responses.
    """
    current_year = datetime.now().year
    for year in range(current_year, current_year - YEAR_LOOKBACK - 1, -1):
        yy = str(year)[2:]  # e.g. 2026 → "26"
        url = f"{CMS_BASE}/rvu{yy}a.zip"
        log.info(f"Probing RVU URL: {url}")
        if _probe_url(url, client):
            log.info(f"Found working RVU URL: {url}")
            return url
    raise RuntimeError(
        f"Could not find a working CMS RVU ZIP URL. "
        f"Tried rvu{{YY}}a.zip for years {current_year} down to "
        f"{current_year - YEAR_LOOKBACK}. "
        f"Check https://www.cms.gov for the current release URL."
    )


def _discover_hcpcs_url(client: httpx.Client) -> str:
    """
    Find the current HCPCS ZIP URL by probing multiple known CMS naming patterns
    in descending year order.

    WHY MULTIPLE PATTERNS:
        CMS has used at least three distinct filename conventions across releases.
        We try the most recently observed patterns first and fall back to older
        ones so the script keeps working even when CMS renames files mid-cycle.

        Patterns tried (most likely first):
          1. alpha-numeric-hcpcs-{YYYY}.zip  — observed current pattern
          2. hcpcs{YY}-anweb.zip             — older 2-digit year pattern
          3. hcpcs{YYYY}anweb.zip            — older 4-digit year pattern
          4. {YYYY}-alpha-numeric-hcpcs-file.zip — original pattern in this script

    SECURITY: we only probe cms.gov and only accept HTTP 200 responses.
    """
    current_year = datetime.now().year
    for year in range(current_year, current_year - YEAR_LOOKBACK - 1, -1):
        yy = str(year)[2:]  # e.g. 2026 → "26"
        candidates = [
            f"{CMS_BASE}/alpha-numeric-hcpcs-{year}.zip",
            f"{CMS_BASE}/hcpcs{yy}-anweb.zip",
            f"{CMS_BASE}/hcpcs{year}anweb.zip",
            f"{CMS_BASE}/{year}-alpha-numeric-hcpcs-file.zip",
        ]
        for url in candidates:
            log.info(f"Probing HCPCS URL: {url}")
            if _probe_url(url, client):
                log.info(f"Found working HCPCS URL: {url}")
                return url
    raise RuntimeError(
        f"Could not find a working CMS HCPCS ZIP URL. "
        f"Tried all known patterns for years {current_year} down to "
        f"{current_year - YEAR_LOOKBACK}. "
        f"Check https://www.cms.gov for the current release URL."
    )


# ---- Download ----

def safe_download(url: str, label: str, client: httpx.Client) -> bytes:
    """
    Download a file from a URL with safety checks.

    WHAT THIS DOES:
        - Checks we got a 200 OK response
        - Verifies the content is actually a ZIP file
          using magic bytes (first 4 bytes = PK\\x03\\x04)
        - Returns raw bytes for further processing

    WHY MAGIC BYTES:
        A server could return HTML (error page) with a .zip
        Content-Type header. We check the actual bytes to
        confirm it's a real ZIP before we try to open it.
    """
    log.info(f"Downloading {label} from {url}")
    response = client.get(url, follow_redirects=True)

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to download {label}: HTTP {response.status_code}"
        )

    content = response.content

    # ZIP magic bytes: PK (0x50 0x4B) followed by 0x03 0x04
    ZIP_MAGIC = b"PK\x03\x04"
    if not content[:4].startswith(ZIP_MAGIC):
        raise ValueError(
            f"Downloaded {label} does not appear to be a ZIP file. "
            f"First 4 bytes: {content[:4]!r}. "
            f"This could mean the CMS URL returned an error page."
        )

    log.info(f"Downloaded {label}: {len(content) / 1024:.1f} KB")
    return content


# ---- Extraction ----

def safe_extract(zip_bytes: bytes, target_pattern: str, label: str) -> bytes:
    """
    Extract ONE specific file from a ZIP archive using an anchored regex pattern.

    WHY ONE SPECIFIC FILE:
        Zip Slip is a real attack where a ZIP contains files with paths like
        ../../etc/passwd that extract outside the target directory. By
        extracting only a file whose basename matches a strict anchored pattern
        we avoid this entirely.

    WHY REGEX NOT EXACT NAME:
        CMS filenames include release-specific suffixes (year, quarter) that
        change between downloads. The pattern pins the structure without
        hardcoding the exact name. re.fullmatch is used — the pattern must
        cover the entire basename, partial matches are rejected.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        all_names = zf.namelist()
        log.info(f"{label} ZIP contains: {all_names}")

        match = next(
            (
                n for n in all_names
                if re.fullmatch(target_pattern, Path(n).name, re.IGNORECASE)
            ),
            None,
        )

        if not match:
            raise FileNotFoundError(
                f"Could not find a file matching '{target_pattern}' in {label} ZIP. "
                f"Available files: {all_names}"
            )

        # Security: reject any path with traversal components
        if ".." in match or match.startswith("/"):
            raise ValueError(f"Suspicious path in ZIP: {match!r}")

        log.info(f"Extracting '{match}' from {label} ZIP")
        return zf.read(match)


# ---- RVU header detection ----

def _find_rvu_header_row(raw_bytes: bytes) -> int:
    """
    Scan first 15 rows of the RVU CSV to find the real header row.

    WHY THIS IS NEEDED:
        The CMS RVU CSV has a multi-line title block at the top (copyright
        notices, release date, column group labels) before the actual data
        header. The real header row contains 'HCPCS' and 'MOD' as column
        names. Using header=0 (pandas default) picks up the title row as
        the header, producing garbage column names.

    FALLBACK:
        Row 9 is the known position in the 2026 QPP release. If the scan
        finds nothing, we fall back to 9 rather than failing hard — the
        file structure is stable across recent releases.
    """
    text = raw_bytes.decode("latin-1", errors="replace")
    for i, line in enumerate(text.splitlines()[:15]):
        upper = line.upper()
        if "HCPCS" in upper and "MOD" in upper:
            return i
    log.warning("Could not auto-detect RVU header row; falling back to row 9")
    return 9


# ---- Processors ----

def process_hcpcs(raw_bytes: bytes, output_path: Path) -> int:
    """
    Parse the HCPCS Excel file and save a clean CSV.

    WHAT WE KEEP:
        - HCPCS code (5 chars, e.g., "99213")
        - Long description (what the procedure is)
        - Short description

    WHAT WE DROP:
        - Effective/termination dates (not needed for RAG)
        - Administrative flags

    WHY CSV OUTPUT:
        The RAG ingest step reads this CSV. CSV is simple,
        auditable, and has no hidden macros (unlike Excel).
    """
    log.info("Parsing HCPCS Excel file...")
    df = pd.read_excel(io.BytesIO(raw_bytes), dtype=str)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    log.info(f"HCPCS columns found: {list(df.columns)}")

    def find_col(df: pd.DataFrame, candidates: list[str]) -> str:
        for c in candidates:
            matches = [col for col in df.columns if c in col]
            if matches:
                return matches[0]
        raise KeyError(f"Could not find column matching any of: {candidates}")

    code_col = find_col(df, ["hcpc", "code"])
    long_desc_col = find_col(df, ["long_description", "long_desc", "description"])
    short_desc_col = find_col(df, ["short_description", "short_desc"])

    clean = pd.DataFrame({
        "code": df[code_col].str.strip().str.upper(),
        "long_description": df[long_desc_col].str.strip(),
        "short_description": df[short_desc_col].str.strip(),
    })

    clean = clean[clean["code"].str.match(r"^[A-Z0-9]{5}$", na=False)]
    clean = clean.drop_duplicates(subset=["code"])

    clean.to_csv(output_path, index=False)
    log.info(f"Saved {len(clean):,} HCPCS codes to {output_path}")
    return len(clean)


def process_rvu(raw_bytes: bytes, output_path: Path) -> int:
    """
    Parse the CMS RVU (Relative Value Unit) file.

    WHAT IS AN RVU:
        Medicare calculates payment as:
        Payment = (Work RVU + Practice Expense RVU + Malpractice RVU)
                  × Geographic adjustment × Conversion factor

        The 2026 conversion factor is $32.74.
        We store the total RVU and compute a reference price.
        If a bill charges 5x+ the Medicare reference price,
        that's a flag for potential overcharge.

    WHY THIS MATTERS FOR ANOMALY DETECTION:
        We're not saying Medicare price = fair price.
        We're saying: "this procedure typically costs X
        under Medicare. Your bill shows Y. That's Z×
        higher. Worth investigating."
    """
    log.info("Parsing RVU file...")

    # Auto-detect the real header row — CMS puts title/copyright rows above it.
    header_row = _find_rvu_header_row(raw_bytes)
    log.info(f"RVU header detected at row {header_row}")

    df = pd.read_csv(
        io.BytesIO(raw_bytes),
        skiprows=header_row,
        header=0,
        dtype=str,
        encoding="latin-1",
    )
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    log.info(f"RVU columns found: {list(df.columns)}")

    # 2026 Medicare conversion factor (dollars per RVU)
    # Source: CMS CY2026 PFS Final Rule
    CONVERSION_FACTOR = 32.74

    def find_col(df: pd.DataFrame, candidates: list[str]) -> str:
        for c in candidates:
            matches = [col for col in df.columns if c in col]
            if matches:
                return matches[0]
        raise KeyError(f"Could not find column matching any of: {candidates}")

    code_col = find_col(df, ["hcpcs_cd", "hcpcs", "code"])
    work_rvu_col = "rvu" if "rvu" in df.columns else find_col(df, ["work_rvu", "wrvu", "physician_work_rvu", "work_rvus"])
    pe_rvu_col = find_col(df, ["fac_pe_rvu", "non_fac_pe_rvu", "pe_rvu"])
    mp_rvu_col = "rvu.1" if "rvu.1" in df.columns else find_col(df, ["mp_rvu", "mal_rvu", "malpractice_rvu", "malpractice"])

    clean = pd.DataFrame({
        "code": df[code_col].str.strip().str.upper(),
        "work_rvu": pd.to_numeric(df[work_rvu_col], errors="coerce"),
        "pe_rvu": pd.to_numeric(df[pe_rvu_col], errors="coerce"),
        "mp_rvu": pd.to_numeric(df[mp_rvu_col], errors="coerce"),
    })

    clean[["work_rvu", "pe_rvu", "mp_rvu"]] = clean[
        ["work_rvu", "pe_rvu", "mp_rvu"]
    ].fillna(0)

    clean["total_rvu"] = clean["work_rvu"] + clean["pe_rvu"] + clean["mp_rvu"]
    clean["medicare_reference_price"] = (
        clean["total_rvu"] * CONVERSION_FACTOR
    ).round(2)

    clean = clean[clean["code"].str.match(r"^[A-Z0-9]{5}$", na=False)]
    clean = clean.drop_duplicates(subset=["code"])

    clean.to_csv(output_path, index=False)
    log.info(f"Saved {len(clean):,} RVU records to {output_path}")
    return len(clean)


# ---- Main ----

def main() -> None:
    """
    Main entry point. Discovers URLs dynamically, then downloads and processes
    both CMS datasets.

    WHY ONE SHARED CLIENT:
        A single httpx.Client reuses the TCP connection across the HEAD probes
        and the final GET download, which is faster and avoids re-negotiating
        TLS repeatedly.

    WHAT HAPPENS IF A DOWNLOAD FAILS:
        We log the error and continue. The app can still run with partial
        data — it just won't have fair price benchmarks if the RVU download
        fails, for example.
    """
    log.info("=" * 60)
    log.info("MedBill Scanner - CMS Data Download")
    log.info("Source: cms.gov (public domain, no license required)")
    log.info("=" * 60)

    results: dict[str, dict] = {}

    # One client for all requests: probe HEADs + final GETs.
    # Timeout: 60s connect, 300s read (files can be several MB).
    with httpx.Client(timeout=httpx.Timeout(60.0, read=300.0)) as client:

        # --- RVU ---
        try:
            rvu_url = _discover_rvu_url(client)
            rvu_bytes = safe_download(rvu_url, "RVU", client)
            rvu_file = safe_extract(
                rvu_bytes,
                target_pattern=r"PPRRVU\d{4}_[A-Za-z]+_QPP\.csv",
                label="RVU",
            )
            count = process_rvu(rvu_file, PROCESSED_DIR / "rvu_rates.csv")
            results["rvu"] = {"status": "ok", "count": count}
        except Exception as e:
            log.error(f"Failed to process RVU: {e}")
            results["rvu"] = {"status": "error", "error": str(e)}

        # --- HCPCS ---
        _hcpcs_out = PROCESSED_DIR / "hcpcs_codes.csv"
        if _hcpcs_out.exists() and _hcpcs_out.stat().st_size > 0:
            log.info(f"HCPCS output already exists ({_hcpcs_out}), skipping download")
            results["hcpcs"] = {"status": "ok", "count": -1, "skipped": True}
        else:
            try:
                hcpcs_url = _discover_hcpcs_url(client)
                hcpcs_bytes = safe_download(hcpcs_url, "HCPCS", client)
                hcpcs_file = safe_extract(
                    hcpcs_bytes,
                    target_pattern=r"HCPC\d{4}(?:_[A-Z]+)?_ANWEB\.(txt|xlsx)",
                    label="HCPCS",
                )
                count = process_hcpcs(hcpcs_file, PROCESSED_DIR / "hcpcs_codes.csv")
                results["hcpcs"] = {"status": "ok", "count": count}
            except Exception as e:
                log.error(f"Failed to process HCPCS: {e}")
                results["hcpcs"] = {"status": "error", "error": str(e)}

    log.info("=" * 60)
    log.info("Download Summary:")
    for key, result in results.items():
        if result["status"] == "ok":
            log.info(f"  ✓ {key.upper()}: {result['count']:,} records")
        else:
            log.error(f"  ✗ {key.upper()}: {result['error']}")
    log.info("=" * 60)

    # Exit with error code if any download failed.
    # WHY: Docker can check exit codes to know if setup succeeded.
    if any(r["status"] == "error" for r in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
