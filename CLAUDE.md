# MedBill Scanner — Claude Code Project Briefing

> This file is read automatically by Claude Code at the start of every session.
> It contains everything needed to continue building without re-explaining context.

---

## What This Project Is

A free, open-source medical bill anomaly detector that helps patients identify
potential overcharges and generate dispute letters. Built to genuinely help people —
not as a demo project.

**Mentor:** Akshay Reddy (Senior SWE at Capital One, AI engineering focus)
**Goal:** Working MVP pushed to GitHub as portfolio piece for AI engineer job search

---

## Architecture (understand this before touching any file)

### MVP Architecture (build this tomorrow)

```
User uploads bill (PDF/image)
        │
        ▼
[1] OCR Service            — pdfplumber + pytesseract, runs LOCAL, no API
        │
        ▼
[2] PII Redactor           — regex only, runs LOCAL, no API
        │                    strips: names, SSN, DOB, insurance IDs
        │                    assert_no_pii_leak() check before next step
        ▼
[3] RAG Retriever          — ChromaDB + sentence-transformers, runs LOCAL
        │                    looks up HCPCS codes + Medicare fair prices
        ▼
[4] ReAct Agent            — ANTHROPIC API called HERE (claude-sonnet-4-20250514)
        │                    reasons across bill + RAG results
        │                    outputs structured anomaly list
        ▼
[5] Dispute Generator      — ANTHROPIC API called HERE
        │                    writes professional dispute letter template
        ▼
[6] React Frontend         — shows anomalies + letter to user
```

### Post-MVP Architecture (do NOT build this during MVP phase)

Image redaction will be added as a second defense layer after MVP ships.
Design is documented here so it is not forgotten or re-debated later.

WHY POST-MVP:
    Image redaction adds complexity and requires careful tuning to avoid
    blacking out CPT codes and dollar amounts we need. MVP text redaction
    covers 95%+ of structured US medical bill formats. Ship first, harden after.

WHY WORTH ADDING LATER:
    Image-first redaction eliminates the regex bypass risk — creative PII
    formatting like "S.S.N: 123 45 6789" can slip past text patterns but
    pixels blacked out in the image cannot be recovered by OCR.

DESIGN (uses only Pillow — already vetted, already in requirements.txt):
```
Image uploaded
        │
        ▼
[1a] Image Pre-Redactor    — Pillow only, runs LOCAL
        │                    blacks out top 15-20% of image
        │                    (patient header region on US medical bills)
        │                    no ML needed — positional heuristic
        ▼
[1b] OCR Service           — runs on pre-redacted image
        │                    PII in header already destroyed at pixel level
        ▼
[2] Text PII Redactor      — regex, belt-and-suspenders layer
        │                    catches any PII outside the header region
        │                    assert_no_pii_leak() before next step
        ▼
        ... (rest of pipeline unchanged)
```

FILES TO ADD (post-MVP only, do not create during MVP):
    backend/services/image_redactor.py   — Pillow-based header blackout
    skills/pii-redaction/image_redactor.py + tests

IMPLEMENTATION NOTE:
    The 15-20% top region heuristic works for common US bill formats
    (hospital UB-04, physician CMS-1500). Make the percentage
    configurable via env var IMAGE_REDACTION_HEADER_PCT=0.18 so it
    can be tuned per bill format without code changes.

---

**Rule:** Anthropic API is NEVER called before PII redaction is complete.
**Rule:** Patient bill data is NEVER persisted to disk anywhere.
**Rule:** CMS reference data (HCPCS + RVU) IS persisted — it's public government data.
**Rule:** Do not implement post-MVP features during MVP phase — scope creep kills ships.

---

## Project Structure

```
medbill-scanner/                     ← git repo root AND data, all on Samsung SSD
                                       full path: /path/to/medbill-scanner
├── CLAUDE.md                        ← you are here
├── docker-compose.yml               ✅ DONE
├── .env.example                     ✅ DONE
├── .gitignore                       ✅ DONE
├── README.md                        ← write last, hiring artifact
│
├── backend/
│   ├── Dockerfile                   ✅ DONE
│   ├── requirements.txt             ✅ DONE
│   ├── main.py                      ✅ DONE
│   ├── config.py                    ✅ DONE
│   ├── mcp_server.py                ✅ DONE
│   │
│   ├── api/
│   │   ├── routes.py                ✅ DONE
│   │   ├── models.py                ✅ DONE
│   │   └── middleware.py            ✅ DONE
│   │
│   ├── rag/
│   │   ├── ingest.py                ✅ DONE
│   │   └── retriever.py             ✅ DONE
│   │
│   ├── services/
│   │   ├── llm_client.py            ✅ DONE
│   │   ├── ocr.py                   ✅ DONE
│   │   ├── pii_redactor.py          ✅ DONE
│   │   ├── anomaly_detector.py      ✅ DONE
│   │   └── dispute_generator.py     ✅ DONE
│   │
│   └── agent/
│       └── react_agent.py           ✅ DONE
│
├── frontend/
│   ├── Dockerfile                   ✅ DONE
│   ├── package.json                 ✅ DONE
│   ├── tsconfig.json                ✅ DONE
│   ├── tailwind.config.js           ✅ DONE
│   ├── postcss.config.js            ✅ DONE
│   ├── nginx.conf                   ✅ DONE
│   ├── public/
│   │   └── index.html               ✅ DONE
│   └── src/
│       ├── App.tsx                  ✅ DONE
│       ├── index.tsx                ✅ DONE
│       ├── index.css                ✅ DONE
│       ├── types/index.ts           ✅ DONE
│       ├── utils/api.ts             ✅ DONE
│       ├── hooks/
│       │   └── useBillAnalysis.ts   ✅ DONE
│       └── components/
│           ├── BillUploader.tsx     ✅ DONE
│           ├── AnomalyReport.tsx    ✅ DONE
│           ├── DisputeLetter.tsx    ✅ DONE
│           └── LoadingSpinner.tsx   ✅ DONE
│
└── scripts/
    └── download_cms_data.py         ✅ DONE — run this first
```

⚠️  IMPORTANT — storage layout:

The entire project — repo AND data — lives on the Samsung SSD.
There is no separate repo location. The SSD IS the repo.

```
/path/to/medbill-scanner/   ← repo root = SSD root
├── backend/             ← application code (git tracked)
├── frontend/            ← application code (git tracked)
├── scripts/             ← utility scripts (git tracked)
├── CLAUDE.md            ← this file (git tracked)
├── docker-compose.yml   ← (git tracked)
├── docker/
│   └── chroma_data/     ← ChromaDB data (gitignored, Docker bind mount)
└── data/
    ├── raw/             ← downloaded CMS ZIPs (gitignored)
    └── processed/       ← cleaned CSVs for ChromaDB (gitignored)
```

docker/ and data/ are gitignored — they exist on SSD but never get committed.
MEDBILL_PROJECT_ROOT in .env must point to this folder for Docker volumes to work.

---

## Context & Subagent Management

This is a large multi-file project. Poor context management causes inconsistent
decisions and lost architectural knowledge mid-session. Follow these rules.

### Spawn a subagent for ALL research tasks — never in main session
Any task that involves searching, looking something up, or investigating must
go to a dedicated subagent. This keeps the main coding session context clean
and focused purely on writing and reasoning about code.

Tasks that MUST use a subagent:
- Vetting a new library against the 6 security checks
- Looking up CMS documentation or column names
- Checking for CVEs or vulnerabilities on osv.dev / snyk.io
- Finding current API reference for any dependency
- Investigating a bug by searching error messages online

Tasks that stay in main session:
- Writing a specific file
- Fixing a bug from terminal output already in context
- Wiring two modules together
- Asking Akshay a clarifying question

### One file per task
Each task targets exactly one file. Do not write ingest.py and retriever.py
in the same task. Finish one, verify it, mark it DONE in CLAUDE.md, then start
the next task fresh.

### Read before write
Before editing any existing file always read it in full first. Never assume
the content matches what was originally planned.

### Reference don't repeat
When referencing earlier decisions, cite the CLAUDE.md section by name rather
than re-explaining. Example: "Per the Security Rules in CLAUDE.md, PII
redaction runs before this call." Repeating context already in CLAUDE.md
wastes context window budget.

### Context reset signal
If you find yourself re-explaining architecture or decisions already documented
here, the context window is getting full. At that point: summarize what was
just completed, update the DONE/TODO markers in this file, and recommend
starting a fresh Claude Code task.

### Update this file as you build
After completing each file, change its status from TODO to ✅ DONE in the
Project Structure section above. This means any new session can immediately
see exactly where the project stands without reading the whole conversation.

---

## Build Order (follow this exactly)

**Phase 1 — Data foundation**
1. `scripts/download_cms_data.py` ✅ already written
2. `backend/rag/ingest.py` — load CSVs into ChromaDB
3. `backend/rag/retriever.py` — semantic search

**Phase 2 — Backend core**
4. `backend/api/models.py` — Pydantic types first (everything depends on these)
5. `backend/services/llm_client.py` — single Anthropic client wrapper
6. `backend/services/ocr.py`
7. `backend/services/pii_redactor.py`
8. `backend/services/anomaly_detector.py`
9. `backend/agent/react_agent.py`
10. `backend/services/dispute_generator.py`
11. `backend/mcp_server.py`
12. `backend/api/routes.py`
13. `backend/api/middleware.py`
14. `backend/main.py`
15. `backend/Dockerfile`

**Phase 3 — Frontend**
16. `frontend/src/types/index.ts`
17. `frontend/src/utils/api.ts`
18. `frontend/src/hooks/useBillAnalysis.ts`
19. `frontend/src/components/` (all 4)
20. `frontend/src/App.tsx`
21. `frontend/Dockerfile`

**Phase 4 — Ship**
22. End-to-end smoke test
23. `README.md`
24. `git push`

---

## Security Rules (non-negotiable, enforce in every file)

### Application-level
1. **PII redaction before ANY Anthropic API call** — no exceptions
2. **File validation on upload** — check magic bytes via python-magic, not just extension
3. **File size limit** — reject anything over MAX_UPLOAD_SIZE_MB (default 10MB)
4. **Allowed types only** — PDF, JPEG, PNG. Reject everything else with HTTP 415
5. **Rate limiting** — 10 requests/minute/IP via slowapi on all /api routes
6. **CORS** — locked to FRONTEND_URL env var, never wildcard *
7. **No secrets in code** — all config via pydantic-settings from .env
8. **No data persistence** — patient bill text lives in memory for one request only
9. **assert_no_pii_leak()** — call this on redacted text before every Anthropic API call
10. **Input validation** — every endpoint uses Pydantic models, never raw dicts

### Docker container-level (already configured in docker-compose.yml)
11. **Non-root user** — backend and frontend run as unprivileged user (uid 1000)
     defined in their Dockerfiles. Never run as root inside containers.
12. **Read-only root filesystem** — containers cannot write to their own filesystem
     at runtime. Immutable filesystem stops attackers from installing tools.
13. **no-new-privileges** — processes cannot escalate to root via setuid binaries
14. **All Linux capabilities dropped** — cap_drop: ALL, cap_add: nothing.
     Web apps need zero kernel capabilities.
15. **tmpfs for /tmp** — RAM-only temp storage, never touches disk, capped at 100MB.
     pytesseract uses /tmp briefly — data is gone when request completes.
16. **Memory limit 512MB per container** — caps blast radius of memory attacks
17. **PID limit 100** — prevents fork bombs from a compromised container
18. **Two isolated networks** — medbill-internal (backend↔chromadb, no internet)
     and medbill-external (frontend↔backend, backend→Anthropic API only).
     ChromaDB has zero internet access. Frontend cannot reach ChromaDB.
19. **Localhost binding only** — ports bound to 127.0.0.1, not 0.0.0.0.
     Backend and frontend are not reachable from other machines on the network.
20. **ChromaDB not exposed to host** — no host port mapping for ChromaDB.
     Only the backend container can reach it via the internal network.

---

## Key Technical Decisions (already made, don't revisit)

| Decision | Choice | Why |
|---|---|---|
| LLM provider | Anthropic Claude only | Simplicity for MVP |
| LLM model | claude-sonnet-4-20250514 | Best balance speed/quality |
| Embeddings | sentence-transformers local | Patient data never leaves Docker |
| Vector DB | ChromaDB | Simple, no infra, Python-native |
| CPT data source | CMS HCPCS + RVU files | Public domain, AMA CPT is copyrighted |
| Fair price benchmark | Medicare RVU × $32.74 conversion factor | Government reference, defensible |
| OCR | pdfplumber (digital) + pytesseract (scanned) | Covers both bill types |
| PII detection | Regex patterns | Fast, auditable, no ML model needed |
| Rate limiting | slowapi | FastAPI-native |
| Frontend | React + TypeScript + Tailwind | Type safety, fast to build |

---

## Environment Variables

See `.env.example` for all variables. Key ones:

- `ANTHROPIC_API_KEY` — required, get from console.anthropic.com
- `CHROMA_HOST` — set to "chromadb" in Docker (container name = hostname)
- `FRONTEND_URL` — CORS allowlist, default http://localhost:3000
- `RATE_LIMIT_PER_MINUTE` — default 10

---

## What's Already Built

- `docker-compose.yml` — all 3 services, isolated network, ChromaDB persistent volume
- `.env.example` — full variable template with comments
- `.gitignore` — secrets, data files, build artifacts all excluded
- `backend/requirements.txt` — all deps pinned and justified
- `scripts/download_cms_data.py` — safe CMS download with ZIP validation, magic byte check

---

## Coding Style Rules

- Every function gets a docstring explaining WHAT, WHY, and any SECURITY NOTES
- No bare `except:` — always catch specific exceptions
- All config from environment, never hardcoded
- Type hints on every function signature
- Log with `logging` module, never `print()`
- Comments explain WHY not WHAT (code shows what, comments show reasoning)

---

## Library Security Policy (strict — no exceptions)

Before adding ANY library not already in requirements.txt, you MUST verify ALL
of the following. If any single check fails, reject the library — find an
alternative or implement the functionality manually. Simple code you wrote and
understand is always safer than a library you haven't vetted.

### 6 mandatory checks for every new library:

**1. Maintenance**
- Last commit within 6 months
- Active maintainers (not a one-person abandoned repo)
- Verify at: github.com/<org>/<repo>/commits

**2. Popularity & trust**
- PyPI: >500k monthly downloads — check pypistats.org
- npm: >500k weekly downloads — check npmjs.com
- Low download count = low community scrutiny = higher malware risk

**3. Known vulnerabilities**
- Check snyk.io/advisor for a risk score
- Check osv.dev for known CVEs
- Any unpatched HIGH or CRITICAL severity = hard reject

**4. Supply chain / typosquatting**
- PyPI package name must exactly match the GitHub repo name
- Example attack: "requets" looks like "requests" but is malware
- Verify the PyPI page maintainer matches the GitHub org
- Never install from a URL, git ref, or anything other than the official registry

**5. Dependency footprint**
- Check transitive dependencies with `pip show <package>`
- Avoid packages that pull in 20+ transitive deps for a simple task
- Every transitive dependency is an additional attack surface you inherit

**6. Source code inspection**
- Does it make unexpected network calls?
- Does it read env vars or files outside its stated purpose?
- Does it use eval(), exec(), or subprocess unexpectedly?
- Red flags: obfuscated code, base64-encoded strings in source, minified Python

### Approved libraries (vetted — use freely):
`fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`, `anthropic`,
`chromadb`, `sentence-transformers`, `pdfplumber`, `Pillow`,
`pytesseract`, `python-magic`, `slowapi`, `limits`, `httpx`,
`pandas`, `python-multipart`

### Hard banned:
- Libraries from outside PyPI/npm official registries
- Any library suggested by a tutorial or AI without running all 6 checks
- curl | bash install patterns
- Unmaintained forks of popular libraries
- Packages with names suspiciously similar to popular ones

---

## When You're Unsure

Ask Akshay. He is the mentor and makes final decisions on:
- Architecture changes
- Any new library (must pass all 6 checks above AND Akshay approves)
- Security tradeoffs
- Scope changes

If a library fails any check, stop immediately, flag it to Akshay,
and propose an alternative before writing any code that depends on it.
