# Data source: CMS.gov HCPCS Level II and Physician Fee Schedule
# Public domain — U.S. government works are not subject to copyright
# Source: https://www.cms.gov
"""
backend/rag/ingest.py
============================================================
PURPOSE:
    Reads the two processed CMS CSV files produced by
    scripts/download_cms_data.py and loads them into ChromaDB
    as a single collection of HCPCS code embeddings.

    This script runs ONCE (at startup / data refresh), not
    on every user request.

HOW TO RUN (inside Docker):
    docker-compose run backend python -m backend.rag.ingest

HOW TO FORCE RE-INGEST (if CMS data was re-downloaded):
    INGEST_FORCE_RELOAD=true docker-compose run backend \
        python -m backend.rag.ingest

WHAT GETS STORED IN CHROMADB:
    Collection: "hcpcs_codes"
    For each HCPCS code:
      - document  (embedded text): "{code}: {long_description}"
      - id: the HCPCS code itself (e.g., "99213")
      - metadata:
          code                    str   "99213"
          short_description       str   "Office/outpatient visit, est"
          medicare_reference_price float  89.03
          total_rvu               float  2.72
          has_price_data          bool  True/False

WHY ONE COLLECTION (not separate HCPCS vs RVU collections):
    The retriever needs both description and price at query time.
    Storing them together avoids a second lookup and keeps
    retriever.py simple. Metadata is returned alongside every
    search result — no extra round-trip.

WHY SENTENCE-TRANSFORMERS LOCAL (not OpenAI embeddings):
    Per architecture decision in CLAUDE.md: CMS reference data
    does NOT contain patient PII, but establishing a principle
    of "nothing leaves Docker" even for embeddings keeps the
    threat model simple. Also: no API cost, no rate limits.

SECURITY NOTES:
    - This file processes CMS public reference data only.
    - No patient data is handled here or at any time during ingest.
    - ChromaDB is on the internal Docker network only.
    - The data directory is mounted read-only inside Docker.
============================================================
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import chromadb
import pandas as pd
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from backend.config import settings

# ---- Logging setup ----
# WHY: Structured logging with timestamps survives Docker log aggregation.
# Print statements are invisible to log shippers and monitoring tools.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---- Constants ----

# ChromaDB collection name. Both ingest.py and retriever.py must use
# the same string — changing this here requires changing retriever.py.
COLLECTION_NAME = "hcpcs_codes"

# Embedding model. all-MiniLM-L6-v2 is:
#   - Fast: ~14,000 sentences/sec on CPU
#   - Small: 80MB model file, fits in our 512MB container memory limit
#   - Accurate enough: 384-dim embeddings catch medical term synonyms
# WHY NOT a larger model (e.g., all-mpnet-base-v2):
#   HCPCS descriptions are short (< 20 words). Larger models do not
#   help meaningfully on short text. Speed and memory matter more here.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# How many documents to send to ChromaDB per add() call.
# WHY 500: ChromaDB processes embeddings in memory. Too large a batch
# risks OOM inside our 512MB container. 500 × 384 floats × 4 bytes = ~768KB
# per batch — well within limits. The full HCPCS file (~8,000 codes)
# completes in ~16 batches.
BATCH_SIZE = 500

# Path to the processed CSV files.
# WHY env var override: in Docker the data is mounted at /app/data.
# On the host for local testing it lives on the SSD. The env var
# lets both environments point to the right place without code changes.
DATA_DIR = Path(os.getenv("MEDBILL_DATA_DIR", "/app/data"))
HCPCS_CSV = DATA_DIR / "processed" / "hcpcs_codes.csv"
RVU_CSV = DATA_DIR / "processed" / "rvu_rates.csv"

_embedding_fn: Optional[SentenceTransformerEmbeddingFunction] = None


# ---- ChromaDB client ----

def get_chroma_client() -> chromadb.HttpClient:
    """
    Create a ChromaDB HTTP client pointed at the running ChromaDB service.

    WHAT: Returns a connected client or raises on failure.

    WHY HttpClient (not EphemeralClient or PersistentClient):
        ChromaDB runs as a separate Docker service ("chromadb" container).
        HttpClient connects to it over the medbill-internal Docker network.
        EphemeralClient is in-memory only (lost on restart).
        PersistentClient writes to a local directory — wrong for Docker.

    SECURITY NOTE:
        CHROMA_HOST defaults to "chromadb" (the Docker container name,
        resolved by Docker's internal DNS). On the host for dev, override
        to "localhost". The port defaults to 8000 (ChromaDB's default).
    """
    host = settings.chroma_host
    port = settings.chroma_port
    log.info(f"Connecting to ChromaDB at {host}:{port}")
    client = chromadb.HttpClient(host=host, port=port)
    # heartbeat() raises immediately if ChromaDB is not reachable,
    # giving a clear error rather than a confusing failure later.
    client.heartbeat()
    log.info("ChromaDB connection confirmed")
    return client


# ---- Embedding function ----

def get_embedding_fn() -> SentenceTransformerEmbeddingFunction:
    """
    Return the sentence-transformers embedding function ChromaDB will use.

    WHAT: Wraps a local sentence-transformers model so ChromaDB calls it
    automatically on every add() and query().

    WHY WE DEFINE THIS HERE (not inside get_or_create_collection):
        The same model name MUST be used at both ingest time and query time.
        Defining it as a module-level function with a shared EMBEDDING_MODEL
        constant makes that contract explicit and easy to verify.
    """
    global _embedding_fn
    if _embedding_fn is not None:
        return _embedding_fn
    log.info(f"Loading embedding model: {EMBEDDING_MODEL}")
    _embedding_fn = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
    return _embedding_fn


# ---- Data loading ----

def load_and_merge_csvs() -> pd.DataFrame:
    """
    Read hcpcs_codes.csv and rvu_rates.csv, then left-join them on 'code'.

    WHAT:
        Returns a DataFrame with one row per HCPCS code containing:
          code, long_description, short_description,
          total_rvu, medicare_reference_price

    WHY LEFT JOIN (not inner join):
        Some HCPCS codes (especially drug codes like J-codes) have no
        RVU entry because Medicare doesn't pay for them under the
        physician fee schedule. We keep them because they still appear
        on patient bills and can still be semantically searched.
        For codes without RVU data, medicare_reference_price = 0.0,
        and we set has_price_data = False so the agent knows.

    RAISES:
        FileNotFoundError: if either CSV is missing — run download_cms_data.py first
        ValueError: if required columns are absent after the merge
    """
    for path in (HCPCS_CSV, RVU_CSV):
        if not path.exists():
            raise FileNotFoundError(
                f"Required CSV not found: {path}\n"
                "Run scripts/download_cms_data.py first to generate it."
            )

    log.info(f"Reading HCPCS codes from {HCPCS_CSV}")
    hcpcs = pd.read_csv(HCPCS_CSV, dtype=str)
    hcpcs.columns = [c.strip().lower() for c in hcpcs.columns]
    log.info(f"  Loaded {len(hcpcs):,} HCPCS rows")

    log.info(f"Reading RVU rates from {RVU_CSV}")
    rvu = pd.read_csv(RVU_CSV, dtype=str)
    rvu.columns = [c.strip().lower() for c in rvu.columns]
    log.info(f"  Loaded {len(rvu):,} RVU rows")

    # Validate we have the columns we expect.
    # WHY explicit check: a subtle column name change in a CMS update
    # would otherwise produce silent NaN data that corrupts the collection.
    required_hcpcs = {"code", "long_description", "short_description"}
    required_rvu = {"code", "total_rvu", "medicare_reference_price"}
    missing_hcpcs = required_hcpcs - set(hcpcs.columns)
    missing_rvu = required_rvu - set(rvu.columns)
    if missing_hcpcs:
        raise ValueError(f"hcpcs_codes.csv is missing columns: {missing_hcpcs}")
    if missing_rvu:
        raise ValueError(f"rvu_rates.csv is missing columns: {missing_rvu}")

    # Keep only the columns we need before merging to avoid name collisions.
    rvu_slim = rvu[["code", "total_rvu", "medicare_reference_price"]].copy()

    # Before — left join drops CPT codes that are in RVU but not HCPCS Level II
    # merged = hcpcs.merge(rvu_slim, on="code", how="left")

    # After — outer join keeps all codes from both datasets
    merged = hcpcs.merge(rvu_slim, on="code", how="outer")

    # Convert numeric columns — they were read as strings for safety.
    # Codes missing from RVU become NaN here; we fill with 0.0 below.
    merged["total_rvu"] = pd.to_numeric(merged["total_rvu"], errors="coerce").fillna(0.0)
    merged["medicare_reference_price"] = pd.to_numeric(
        merged["medicare_reference_price"], errors="coerce"
    ).fillna(0.0)

    # # Fill any remaining string NaNs with empty string for ChromaDB safety.
    # merged["short_description"] = merged["short_description"].fillna("")
    # merged["long_description"] = merged["long_description"].fillna("")
    merged["long_description"] = merged["long_description"].fillna(merged["short_description"].fillna(""))
    merged["short_description"] = merged["short_description"].fillna("")

    log.info(
        f"Merged dataset: {len(merged):,} codes "
        f"({(merged['medicare_reference_price'] > 0).sum():,} with price data)"
    )
    return merged


# ---- Document building ----

def build_chroma_documents(
    df: pd.DataFrame,
) -> tuple[list[str], list[str], list[dict]]:
    """
    Convert the merged DataFrame into ChromaDB-ready (ids, documents, metadatas).

    WHAT:
        ids        — HCPCS code string, e.g., "99213"
        documents  — text that gets embedded, e.g., "99213: Office or other..."
        metadatas  — dict of numeric and string fields returned alongside results

    WHY CODE IN THE DOCUMENT TEXT:
        The document text is what semantic search runs on. Putting the code
        in the text means a query for "99213" will match the embedding
        AND the description allows topic queries like "office visit" to match.
        Without the code in the text, exact code lookups would miss.

    WHY NOT EMBED PRICE DATA:
        Price is a number — meaningless in embedding space. It lives in
        metadata so the retriever can return it as-is without any arithmetic
        happening in the embedding layer.

    WHY has_price_data FLAG:
        The ReAct agent needs to know whether to trust a price comparison
        or skip it. A 0.0 price could mean "free" (rare) or "no data"
        (common for drug codes). The flag makes intent explicit.

    CHROMADB METADATA CONSTRAINT:
        ChromaDB metadata values must be str, int, float, or bool.
        No lists, no None. We ensure this for every field.
    """
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for _, row in df.iterrows(): #TODO df.iterrows() which is slow for large DataFrames,CMS data ever grows significantly, replace with vectorized operations
        code = str(row["code"]).strip().upper()
        long_desc = str(row["long_description"]).strip()
        short_desc = str(row["short_description"]).strip()
        total_rvu = float(row["total_rvu"])
        ref_price = float(row["medicare_reference_price"])

        # Document text: rich enough for semantic search, concise enough to embed fast.
        doc_text = f"{code}: {long_desc}" if long_desc else code

        ids.append(code)
        documents.append(doc_text)
        metadatas.append(
            {
                "code": code,
                "short_description": short_desc,
                "total_rvu": total_rvu,
                "medicare_reference_price": ref_price,
                # Explicit flag so the agent can distinguish "price is 0"
                # from "no price data available"
                "has_price_data": total_rvu > 0.0,
            }
        )

    return ids, documents, metadatas


# ---- Main ingest logic ----

def ingest(force_reload: bool = False) -> int:
    """
    Full ingest pipeline: load CSVs → build documents → upsert to ChromaDB.

    WHAT:
        Returns the number of documents in the collection after ingest.

    WHY IDEMPOTENT (check before reinserting):
        Running ingest twice should not double the data. ChromaDB's
        upsert() handles this for individual documents, but we skip the
        work entirely if the collection already has data and force_reload
        is False. This makes startup fast on subsequent runs.

    WHY UPSERT (not add):
        upsert() creates new documents OR updates existing ones by id.
        This means re-running after a CMS data update replaces stale
        records rather than creating duplicates or raising an error.

    ARGS:
        force_reload: if True, delete and recreate the collection even if
                      it already has data. Use when CMS data is refreshed.
    """
    client = get_chroma_client()
    embedding_fn = get_embedding_fn()

    # get_or_create_collection is idempotent — safe to call on every startup.
    # WHY pass embedding_fn here: ChromaDB stores the function config alongside
    # the collection so every future add/query automatically uses the same model.
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
        # WHY cosine distance:
        #   Cosine similarity is standard for sentence embedding comparisons.
        #   It is length-invariant, which matters because HCPCS descriptions
        #   vary from 3 words ("Glucose test") to 20+ words.
    )

    existing_count = collection.count()
    log.info(f"Collection '{COLLECTION_NAME}' currently has {existing_count:,} documents")

    if existing_count > 0 and not force_reload:
        log.info(
            "Skipping ingest — collection already populated. "
            "Set INGEST_FORCE_RELOAD=true to rebuild."
        )
        return existing_count

    if existing_count > 0 and force_reload:
        log.info("INGEST_FORCE_RELOAD=true — deleting and recreating collection")
        client.delete_collection(name=COLLECTION_NAME)
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )

    # Load and prepare data
    df = load_and_merge_csvs()
    ids, documents, metadatas = build_chroma_documents(df)
    total = len(ids)
    log.info(f"Prepared {total:,} documents for ingest")

    # Batch upsert into ChromaDB.
    # WHY batches: sending all ~8,000 codes at once would spike memory
    # during embedding computation. Batches of 500 keep peak RAM low.
    inserted = 0
    for start in range(0, total, BATCH_SIZE):
        end = min(start + BATCH_SIZE, total)
        batch_ids = ids[start:end]
        batch_docs = documents[start:end]
        batch_meta = metadatas[start:end]

        collection.upsert(
            ids=batch_ids,
            documents=batch_docs,
            metadatas=batch_meta,
        )

        inserted += len(batch_ids)
        log.info(f"  Upserted {inserted:,}/{total:,} documents")

    final_count = collection.count()
    log.info(
        f"Ingest complete. Collection '{COLLECTION_NAME}' now has "
        f"{final_count:,} documents."
    )
    return final_count


# ---- CLI entry point ----

def main() -> None:
    """
    CLI entry point for running ingest as a script or Docker one-off command.

    Usage:
        python -m backend.rag.ingest
        INGEST_FORCE_RELOAD=true python -m backend.rag.ingest
    """
    log.info("=" * 60)
    log.info("MedBill Scanner — RAG Ingest")
    log.info(f"Embedding model : {EMBEDDING_MODEL}")
    log.info(f"ChromaDB host   : {settings.chroma_host}")
    log.info(f"Data directory  : {DATA_DIR}")
    log.info("=" * 60)

    force = os.getenv("INGEST_FORCE_RELOAD", "false").lower() == "true"
    if force:
        log.info("Force reload enabled — existing collection will be replaced")

    try:
        count = ingest(force_reload=force)
        log.info(f"Done. {count:,} codes available for RAG retrieval.")
    except FileNotFoundError as exc:
        log.error(str(exc))
        sys.exit(1)
    except chromadb.errors.ChromaError as exc:
        log.error(f"ChromaDB error: {exc}")
        sys.exit(1)
    except Exception as exc:
        log.error(f"Unexpected error during ingest: {exc}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
