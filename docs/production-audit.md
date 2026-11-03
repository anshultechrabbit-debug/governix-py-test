# Governix production-hardening audit

Date: 2026-10-30. Builds on `docs/rag-audit.md` (2026-10-05), which traced the RAG pipeline in depth; this audit
checks what has changed since (answer-cache versions v36–v73, see `AGENTS.md`) and covers the rest of the platform.

How it was done: by reading the code. No tests, suites or questions were run (AGENTS.md: they cost OpenAI credit and
need the owner's go-ahead). "Verified" below means the line was read; nothing here is a measured result.

---

## 1. Findings by severity

### CRITICAL

**C1. CSRF is only checked when the request chooses to send the header.**
`app/modules/auth/dependencies.py:60`: `if request.headers.get("X-CSRF-Token"): require_csrf(...)`. A
cookie-authenticated POST/PUT/PATCH/DELETE that simply omits the header is accepted. `/auth/refresh`
(`auth/router.py:71`) checks CSRF only when the CSRF cookie happens to be present.
- SameSite=Lax does not cover this deployment: the site is served from an IP
  (`89.116.122.215:7001`), and every other port on that IP is *same-site*. Lax also allows top-level GET navigations.
- `app/tests/integration/test_auth.py:86` expects `CSRF_FAILED` for a cookie-only POST. Introduced by commit
  `1c6523fa` ("support bearer tokens and lax cookie samesite").
- Fix (no client impact): require CSRF whenever the access token came from the cookie. A Bearer token is not sent
  by the browser on its own, so Bearer clients need no CSRF header.

**C2. CORS allows every origin, with credentials.**
`app/core/config.py:148` puts `"*"` in `CORS_ORIGINS`, and `app/main.py:144` sets `allow_credentials=True`,
`allow_headers=["*"]`, `expose_headers=["*"]` and `allow_private_network=True`.
- With credentials and `"*"`, Starlette answers a request that carries cookies with that request's own origin.
  Any page whose request carries the session (same-site pages today, and every site if cookies ever become
  SameSite=None) can read authenticated responses: answers, documents, the audit log.
- The defaults also hard-code LAN addresses.
- The web app is served on the same origin through nginx (`/api`), so it needs no CORS at all.
- Which external client needed `"*"` is a decision for the owner (see §4).

**C3. Retrieval granularity (rag-audit RC3) is still open.**
Chunks hold 7–8 unrelated rules, and no rule-level unit index exists (no units table in `migrations/`).
- The 2026-10-05 measurement ranked the right chunk 492–2,786 in the vector lane for 5 sampled failures, and found
  the right rule in the top 10 for 4/20 questions at chunk level versus 20/20 at rule level.
- Gate, prompt and validation changes cannot fix a rule that never reaches the candidate set.
- It needs a schema change, a backfill job and re-embedding, which AGENTS.md reserves for the owner's approval.

**C4. No current, reproducible quality baseline.**
- The 333-question CSV that `run_csv_suite.py` grades (`rag_test_questions (2).csv`) is not in the repository.
- The last full measurement (94 correct / 53 partial / 45 wrong / 141 no answer) predates v36–v73.
- `suite_results.txt` (NEP suite) is from 2026-09-29.
- Without a fresh run, no improvement since then can be claimed, including v73.

### HIGH

**H1. The auth documentation contradicts the code.**
- `AGENTS.md` and the `cookies.py` docstring say SameSite=Strict and that an `Authorization` header is ignored.
- The code accepts `Bearer` on every route and on refresh, and hard-codes `samesite="lax"`. The
  `AUTH_COOKIE_SAMESITE` setting exists but is never read.
- `test_auth.py:80-83` asserts a Bearer header is rejected, so it no longer matches the code (by reading).

**H2. Per-query cost and model trace are incomplete.**
- The `ai.query` audit event (`rag/service.py`, `_audit`) stores `usage` for the final answer call only.
- `model_calls` has each call's name and time but no tokens.
- Nothing records the prompt version or an estimated cost.
- `ai_usage` rows (`infrastructure/ai/usage.py`) are not linked to the query that made them.
- So the cost per query, and which prompt produced an answer, cannot be read back.

**H3. The reranker is lexical by default.**
`RERANKER_PROVIDER=local` is word overlap with prefix stemming. An API reranker adapter exists
(`infrastructure/ai/reranker/api.py`), unconfigured. Per rag-audit, re-measure after C3 before paying for a
cross-encoder.

**H4. Only PDF is ingested.**
DOCX and XLSX are not accepted (`documents/service.py` stores `application/pdf`; extraction is PyMuPDF). Scanned
PDFs are covered by OCR (`workers/ocr`).

**H5. EMI cannot be computed from a rate.**
The calculator (`rag/calculate.py`, Decimal + `ast`) has no powers, by design. A policy that states only a rate and
a tenure, with no EMI table, cannot get an EMI worked out. Everything else the brief lists (fees, penalties,
FOIR/EMI room, LTV, caps, totals) is supported.

### MEDIUM

**M1. Latency.**
- v73 adds one small rewrite call to every question.
- Refusals still take the longest path: earlier versions one by one, then a clarify call and a full re-run
  (rag-audit RC9).
- p95 under load was 32 s on 2026-10-05.

**M2. Conflicts are only detected for figures.**
`evidence.detect_conflicts` finds amended clauses and differing figures for one subject. It does not detect two
sources that differ in permission ("permitted" against "not permitted"). Those rely on the model's `conflicts`
field.

**M3. The v73 changes (framing words, document list, overview) are untested.** Their unit tests were written, not
run.

### LOW

**L1. Build output and data in git.** Compiled `__pycache__/*.pyc` files and an uploaded 95k-line PDF are tracked
(commit `1c6523fa`).

**L2. Destructive scripts at the repository root.** `reset_db.py` and `delete_documents.py` sit beside the
deployable code. Keep them out of the deploy artefact.

---

## 2. What works and is kept

Verified in code unless marked (doc), meaning it is documented in AGENTS.md or `docs/validation.md`.

- **Tenancy and ACL:** `auth/acl.visible_clause` sits inside every retrieval lane's SQL and is re-checked when
  citations are verified. Isolation tests exist for documents, retrieval, RAG, cache and tenancy (`app/tests/security/`).
- **Hybrid retrieval:**
  - Exact lane: clauses, identifiers, pages, chapters.
  - IDF-weighted keyword lane.
  - HNSW vector lane.
  - Section lane.
  - Weighted RRF fusion.
  - Policy aliases derived from the documents (`search/policy_names.py`).
- **Versions:**
  - Half-open effective ranges, chosen by effective date, not upload date.
  - Per-version search for change-over-time and dates questions (doc).
  - Stored deterministic diffs; amendments attached through the same ACL.
- **Answer validation:**
  - Citations exist and are re-verified in the DB.
  - Figures are grounded.
  - Word support, subject, variant, version and polarity checks.
  - Absence statements rejected.
  - `insufficient_evidence` honoured (v36).
  - Meaning check and reasoning check, both failing closed.
  - Summary check.
- **Calculations:**
  - Decimal arithmetic.
  - The model's expressions recomputed, every figure grounded, wrong results reported.
  - The result restated by code (v69–v70).
- **Prompt and context injection:** handled by grammar in questions, documents and history (v57).
- **Async ingestion:** a local queue or Celery+Redis, staged pipeline workers, embedding cache, batch embeddings.

## 3. Brief coverage

| Phase | State | Notes |
|---|---|---|
| 2 Retrieval | Partial | Hybrid + exact + filters + aliases done; rule-level units (C3) and reranker (H3) open |
| 3 Versions/amendments | Mostly done | Clause-level supersession is per section diff, not per rule |
| 4 Calculations | Mostly done | EMI from rate missing (H5) |
| 5 Citations | Done | Document, version and ACL re-verified; page from stored provenance |
| 6 Multi-stage validation | Done | Retries bounded (one reasoning retry, one compute call) |
| 7 Prompts | Partial | Prompts are modular; no prompt version is recorded (H2) |
| 8 Security | **Gaps** | C1, C2, H1 |
| 9 Performance | Partial | Indexes and caching in place; no benchmark since 2026-10-05 (M1) |
| 10 Testing/eval | **Gap** | Dataset missing from repo; no current baseline (C4) |
| 11 Observability | Partial | Request id, timings, chunk ids, plan, checks recorded; tokens per call, cost, prompt version missing (H2) |

## 4. Plan

Done now, with no decision needed (generic, no schema change, compatible with the frontend and with Bearer clients):

1. **C1:** CSRF required for every cookie-authenticated unsafe request, and for a refresh by cookie.
2. **H2:** every model call's tokens in `model_calls`, total usage and estimated cost per query, and a prompt version
   hash, all in the `ai.query` audit event. Prices are configurable; with no price set, no cost is estimated.

Needs the owner:

3. **C2 CORS:** which external client (if any) needs cross-origin access? The proposal is no `"*"`, origins from
   `CORS_ORIGINS` in `.env`, and the same-origin web app unaffected.
4. **C3 rule-level index:** approve the schema change, backfill and re-embedding (rag-audit stage 3: about 1–3.5 GB
   of new index, about $2 of embeddings).
5. **C4 evaluation:** where the 333-question CSV is, and approval to run `run_csv_suite.py` (OpenAI cost) for a fresh
   baseline. Also approval to run `pytest app/tests` (local models only, separate `_test` database).

Next, after those: H5 (deterministic EMI from rate), M2 (permission conflicts), H3 (reranker measurement), H4
(DOCX/XLSX ingestion), L1 (untrack build output).
