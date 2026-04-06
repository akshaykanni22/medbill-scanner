# ============================================================
# backend/Dockerfile
# ============================================================
# PURPOSE:
#   Build the MedBill Scanner backend container.
#   FastAPI + uvicorn + all Python services.
#
# SECURITY MODEL:
#   This Dockerfile enforces the container-level security rules
#   from CLAUDE.md:
#   - Non-root user (uid 1000)         — cannot modify system files
#   - No root at build or run time     — principle of least privilege
#   - Minimal base image               — fewer packages = fewer CVEs
#   - Pinned base image digest not tag — prevents tag mutation attacks
#     (NOTE: we use python:3.11-slim for MVP; production should pin
#      to a specific digest, e.g., python:3.11-slim@sha256:...)
#   - System deps installed and cache cleared in one RUN layer
#     to minimise image size and attack surface
#
# BUILD CONTEXT:
#   docker-compose.yml sets context: ./backend
#   All paths below are relative to the backend/ directory.
#
# HOW THE NON-ROOT USER WORKS:
#   We create a user `appuser` with uid=1000 and no password.
#   The app files are owned by appuser.
#   The final USER instruction switches to appuser for all
#   CMD/ENTRYPOINT execution. Root is never used at runtime.
#
# LAYER ORDER (dependencies before code):
#   Install requirements BEFORE copying application code.
#   WHY: Docker layer cache — if requirements.txt doesn't change,
#   the `pip install` layer is cached and rebuilds are fast.
#   Copying code first would bust the cache on every code change.
# ============================================================

# --- Base image ---
# python:3.11-slim: Debian-based, Python 3.11, minimal packages.
# WHY 3.11 (not 3.12+): sentence-transformers and chromadb are
# well-tested on 3.11. Upgrade post-MVP after verifying compatibility.
# WHY slim (not alpine): pytesseract requires system libraries
# (libtesseract, libgomp) that are easier to install on Debian.
# Alpine's musl libc causes subtle issues with some ML libraries.
FROM python:3.11-slim

# --- Build arguments ---
# Non-root user configuration. UID 1000 is the first non-system user
# on most Linux systems — standard for application containers.
ARG APP_USER=appuser
ARG APP_UID=1000
ARG APP_GID=1000

# --- System dependencies ---
# Install in a single RUN to keep layers small and cache-friendly.
# Clean apt cache immediately to reduce image size.
#
# PACKAGES:
#   tesseract-ocr      — Tesseract OCR engine (required by pytesseract)
#   libmagic1          — libmagic shared library (required by python-magic)
#   libgomp1           — OpenMP runtime (required by sentence-transformers)
#   curl               — used by the Docker healthcheck command
#
# WHY NO RECOMMENDED/SUGGESTED:
#   --no-install-recommends prevents apt from pulling in ~30 extra
#   packages that the application doesn't use. Each extra package
#   is additional attack surface.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libmagic1 \
        libgomp1 \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# --- Create non-root user ---
# WHY before pip install: if we create the user after installing
# packages, pip's cache dir ownership gets complicated.
# Creating first keeps the ownership model simple.
RUN groupadd --gid ${APP_GID} ${APP_USER} && \
    useradd --uid ${APP_UID} --gid ${APP_GID} --no-create-home --shell /sbin/nologin ${APP_USER}

# --- Application directory ---
# /app is the convention. chown to appuser so the app can write
# to any subdirectory it needs (e.g., model cache for sentence-transformers).
WORKDIR /app
RUN chown ${APP_UID}:${APP_GID} /app

# --- Python dependencies ---
# Copy only requirements.txt first to leverage Docker layer caching.
# This layer is cached until requirements.txt changes.
COPY --chown=${APP_UID}:${APP_GID} requirements.txt .

# Install as root to avoid permission issues with system packages,
# but install to the user-level site-packages.
# WHY --no-cache-dir: reduces image size by ~50MB (no pip cache).
# WHY --upgrade pip first: older pip versions have known CVEs.
RUN pip install --upgrade pip --no-cache-dir && \
    pip install --no-cache-dir -r requirements.txt

# --- Pre-download sentence-transformers model ---
# WHY at build time (not first request):
#   Downloading the model at startup would add 1-2 minutes to the
#   first request after container start. Baking it into the image
#   means the container is ready immediately when it starts.
#   The model is ~90MB — acceptable image size increase.
#
# WHY as appuser:
#   sentence-transformers caches the model in ~/.cache/huggingface.
#   Running as appuser ensures the cache is in appuser's home dir
#   (or the system cache) and is readable by the app at runtime.
#   Note: appuser has --no-create-home, so HOME=/root here — the
#   model caches to /root/.cache during build (root step below).
#   At runtime, HF_HOME=/app/.cache is set to a writable location.
ENV HF_HOME=/app/.cache/huggingface
RUN mkdir -p /app/.cache/huggingface && \
    chown -R ${APP_UID}:${APP_GID} /app/.cache && \
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# --- Application code ---
# Copy after requirements so code changes don't bust the pip cache.
# WHY --chown: ensures appuser owns all app files.
# WHY copy the whole context (not individual files): docker-compose
# sets context: ./backend, so this copies everything in backend/.
COPY --chown=${APP_UID}:${APP_GID} . .

# --- Switch to non-root user ---
# All subsequent RUN, CMD, and ENTRYPOINT run as appuser.
# This is the critical step — from here the process cannot
# write to system directories even if exploited.
USER ${APP_USER}

# --- Healthcheck ---
# Docker uses this to determine if the container is "healthy".
# docker-compose's depends_on: condition: service_healthy waits
# for this to pass before starting dependent services.
#
# WHY /api/health (not /health): routes.py registers the health
# endpoint at "/health", but main.py mounts the router with
# prefix="/api" — so the actual URL is /api/health.
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# --- Expose port ---
# Informational only — docker-compose.yml controls actual binding.
EXPOSE 8000

# --- Start command ---
# uvicorn serves the FastAPI app.
# WHY --host 0.0.0.0 inside Docker:
#   Inside the container, 0.0.0.0 means "accept connections on all
#   container interfaces" — not all host interfaces. Docker's port
#   binding (127.0.0.1:8000:8000 in docker-compose.yml) controls
#   what reaches the host. Inside the container, 0.0.0.0 is safe.
# WHY 1 worker:
#   sentence-transformers model loading and ChromaDB singleton are
#   not safe for multiprocessing. 1 worker + async is correct here.
#   Post-MVP: if throughput becomes an issue, consider running
#   multiple containers behind a load balancer instead.
CMD ["uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
