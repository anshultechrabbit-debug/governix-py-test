# Governix: notes for coding agents

Governix is a banking-policy RAG platform. FastAPI backend (`app/`), sync SQLAlchemy with psycopg 3,
Alembic migrations, Postgres 18 with pgvector 0.8, and a React + Tailwind + Redux Toolkit frontend
(Vite, in `frontend/`). Governix issues its own JWTs; there is no external SSO.

## How the user wants work done

- Build phase by phase from the spec. Never generate the whole app at once.
- Fixes must be generic: users can upload any PDF and use any short form of its terms. Never add
  per-tenant or per-document keyword or synonym lists. Derive aliases from the document data.
  `validation.SYNONYM_GROUPS` is for general English synonyms only.
- Keep refusal safety. Never loosen a check in a way that lets an unsupported answer through.
- Ask before changing the DB schema or building the rule-level unit index (stage 3 of `docs/rag-audit.md`).
- Don't run tests, suites or probe scripts unless the user asks. The user tests in the UI and is
  careful with OpenAI credit. That includes `run_suite.py`, `run_csv_suite.py`, `pytest` and any script
  that embeds or asks a question.
  - `app/tests` uses `LLM_PROVIDER=local` and `EMBEDDING_PROVIDER=local`, plus a separate
    `<db>_test` database, so it makes no OpenAI calls. Still ask first.
  - Read code to check a change instead of running it.
- Fix types of question, not single questions. When a check rejects a correct answer, fix the check's
  rule, not a word list.
- Report changes under these headings: What changed / Files / DB / API / Tests / Risks.
- When answer behaviour changes, bump `ANSWER_CACHE_VERSION` in `app/modules/rag/service.py` and add a
  comment line for it. Otherwise cached answers (10 min) hide the change.

## Environment

- `.env` uses `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-4o-mini`, and OpenAI `text-embedding-3-small`
  (1536-d) embeddings. The OpenCode Zen settings (`space-bunny-free`) stay in `.env` as the fallback
  for when OpenAI credit runs out. Only that free model works through the API: the others return
  403/402, so don't work around that.
- Documents ingested while OpenAI embeddings were failing get `local-hashing-v1-1536` vectors. The
  vector lane only uses chunks whose `embedding_model` matches the current model, so those documents
  are reachable by keyword only. To fix: `.venv/bin/python -m app.cli reembed --organization <ORG_ID>`.
- Never put the database password on a command line. Use the app's engine from Python with
  `PYTHONPATH=.`, via `create_db_engine(get_settings())`.
- Test data:
  - Org `d81d8ac2-7d9c-490b-bf60-c816300b2ae7`: Home Loan Guide v1-v8 (one policy, 8 versions, about
    10 chunks each), plus small QA policies (Compliance, Gold Loan, HR & Leave, Mortgage, Operations).
  - Org `2fb01ee1-3e0d-44ec-bea0-ad57ca66332f`: the 333-question CSV (Home Loan / Personal Loan /
    KYC-AML), baseline in `docs/rag-audit.md`.
  - `run_suite.py` holds the National Electricity Plan suite.

## Auth sessions and API errors

- Tokens are never in a response body. The web app uses cookies (`app/modules/auth/cookies.py`); since commit
  `1c6523fa` a non-browser client may also send `Authorization: Bearer <access>` (and a Bearer refresh token to
  `/auth/refresh`). `governix_access` (JWT) and `governix_refresh` are HttpOnly; `governix_csrf` is readable. All
  are SameSite=Lax, and `Secure` follows the request scheme (`AUTH_COOKIE_SECURE`), because the live site is plain HTTP.
- Every cookie-authenticated POST/PUT/PATCH/DELETE must send `X-CSRF-Token` equal to the `csrf` claim in its access
  JWT; a cookie refresh compares it with the CSRF cookie, and logout too. A missing header is refused, never
  skipped (SameSite=Lax does not protect an IP-addressed site: every port is the same site). Bearer requests need no
  CSRF header. The frontend client adds it.
- Tabs share the refresh cookie. A token rotated less than 30 s ago gets `TOKEN_ROTATED` (another tab
  won the race) instead of revoking the family.
- **Two-step sign-in (2026-11-04)**: optional for everyone, set up from the account menu (`TwoStepDialog.tsx`).
  - TOTP (`auth/mfa.py`, pyotp): 6 digits/30 s, ±1 step, never the same step twice (`users.mfa_last_step`). The secret
    is Fernet-encrypted in `users.mfa_secret` with a key from `MFA_ENCRYPTION_KEY` (in `.env`; changing it disables
    every authenticator). `mfa_enabled_at` set = on; a secret without it = setup not confirmed.
  - Login with it on answers `mfa_required: true` and sets only the HttpOnly `governix_mfa` challenge cookie
    (`MFA_CHALLENGE_MINUTES`, bound to `token_version`); `/auth/mfa/verify` takes the app code or a backup code and
    issues the session. Wrong codes count as failed logins; `/auth/mfa/*` is also rate-limited.
  - Login lockout (re-enabled 2026-11-16): `LOGIN_MAX_FAILED_ATTEMPTS` (5) failed passwords or codes lock the account
    for `LOGIN_LOCKOUT_MINUTES` (15), `ACCOUNT_LOCKED`; checked at the password step and at `/auth/mfa/verify`.
  - 10 backup codes (`mfa_recovery_codes`, SHA-256), shown once at setup or renewal. They only sign in: turning it
    off needs the password plus an app code (never a backup code).
  - Security alerts (`auth/alerts.py`, notification `SECURITY_ALERT`): set up, turned off, reset, backup code used ->
    the person and `account_managers` (their Branch Manager and Org Admins; for an Org Admin also Master Admins).
    That is the defence against a thief who knows the password of an account without an authenticator and sets up
    their own phone (the user chose alerts with no waiting period). An admin who manages the user (`UserService._can_manage`) resets it via `/users/{id}/reset-mfa`
    (signs them out everywhere, audited). Tests: `app/tests/integration/test_two_step_sign_in.py`.
- **Important actions need a fresh authenticator code, every time** (`auth/step_up.py`, `StepUp`): the endpoint calls
  `confirm_code("<action>")` before doing anything; the code comes in `X-Authenticator-Code`. No two-step sign-in ->
  403 `STEP_UP_SETUP_REQUIRED` (refused until set up); no/wrong/used code -> 403 `STEP_UP_REQUIRED` / `STEP_UP_INVALID`
  / `STEP_UP_CODE_USED`. App codes only (not backup codes); the spent step is saved with the action, so one code
  approves one action. The frontend client (`api/client.ts`) asks via `StepUpPrompt` and resends; pages need nothing.
  Covered: add user; change a user's email/role/org/branch/department (a value that really changes:
  `users/router.sensitive_changes`; forms send the whole user); delete user; reset someone's password or two-step;
  create (2026-11-17), suspend/reactivate or delete an organization; delete a branch or department (the `confirm=true` call,
  not the preview); publish/disable a portal; withdraw/restore a policy version; delete a policy or document; add
  support staff. Turning a user active / inactive needs no code (2026-11-17, the user's choice). `STEP_UP_ENABLED`
  (refused off in production) is off in the test suite except `test_step_up.py`.
- Error bodies: `error.code` is the HTTP status (401, 404, ...) and `error.reason` the stable text
  identifier (`INVALID_CREDENTIALS`, `VERSION_CONFLICT`, ...). The frontend's `ApiError.code` holds the
  reason.

## Input rules (2026-11-05, `app/core/inputs.py`)

- **Optional types take their length as `AtMost(n)`** (2026-11-16): `address: Annotated[MaybeText, AtMost(2000)] = None`,
  never `MaybeText = Field(max_length=2000)`. Pydantic applies a field's `max_length` after the blank-to-None cleaning,
  to the whole value, and `len(None)` is a TypeError: a 500 instead of a 422 (it broke POST /organizations with an
  empty address/phone/website). `Phone` and `Website` use it inside. Required types (`Name`, `Text`, ...) keep `Field`.

- Every request schema uses the shared types, never a bare `str` for user text: `PersonName` (letters, spaces, ' - .),
  `Name` (one line, a letter or digit, no < >), `Line`, `Text` (multi-line), `Phone` (7-15 digits), `Website`
  (http/https), `Tag` (no commas), `NewPassword`, and `MaybeName/MaybeLine/MaybeText` (blank = not given). Values are
  cleaned before the length check, so "   " never passes. Each type carries `x-rules` into the OpenAPI schema.
- Password policy (`password_problems`): 12-128 characters, a capital, a small letter, a digit, a symbol, no space at
  either end, no character 4+ times in a row, not the person's name or email local part (`check_password` in the
  services, `WEAK_PASSWORD`). Applies whenever a password is set, never at sign-in. `generate_temporary_password`
  always meets it. The frontend mirrors it in `components/PasswordRules.tsx`.
- 422 messages name the field and the rule ("New password must include a digit (0-9)."), `error_handlers._readable`.
- `docs/governix-input-validations.pdf` is generated from the code (routes, schemas, permissions, service rules).

## Support Center (`app/modules/tickets/`, `/support` API; replaced complaints and `/tickets` on 2026-11-03)

- **Queues** (`queues.py`): Branch Support (per branch), Head Office Support (per bank), Technical Support
  (Governix, platform). Members are `SupportStaff` profiles.
- **Staff are their own accounts (2026-11-16)**: every new staff member is a `support_agent` account created on the
  Support Staff page (`StaffService.create(new_account=)`, temporary password shown once), only the `support`
  permission. Governix's have no organization; a bank's are in that bank (Head Office) or bank + branch (Branch
  Support): `users.role_scope` allows it (migration `7d1e4a2c9b58`). `new_account` is required at every level:
  nobody, platform included, puts an existing account on duty (no `user_id`, no `/support/staff/candidates`).
  Removing a bank staff member also soft-deletes the account. Their managers reset a staff account's password from
  the Support Staff page (`POST /support/staff/{id}/reset-password`, temporary password shown once, signed out
  everywhere); ordinary users' passwords are reset from Users, by the person who manages them.
  Its role, bank and branch never change through `/users` (`SUPPORT_STAFF_ACCOUNT`); its security alerts go to
  the branch's Branch Manager and the Org Admins. Profiles put on ordinary users before this still work. Managers: Branch Manager, Org Admin
  (Head Office and oversight of every branch), Master Admin; a `lead` profile also manages its queue.
- **Flow**: create -> `awaiting_confirmation` with an AI `routing` recommendation (`triage.py`, `TRIAGE_PROMPT`;
  local stand-in or outage -> `_from_text`: title (x2) and description words against each team's general description
  plus the org's category names, category breaks ties; then the category's route; always confidence 0.5, preselected
  but marked uncertain) -> the creator confirms or switches the team (`/confirm`) -> `open`, SLA starts,
  auto-assigned. Drafts are visible to their creator only. Policy-content questions are offered the AI Assistant
  (`routing.redirect`).
- **Where it starts** (`TicketService._level_for`): technical -> platform; branch -> the User's branch, or Head
  Office for a Branch Manager / Org Admin. The model never chooses a queue or a person; skills it returns are
  kept only if a staff member has that tag.
- **Assignment** (`assignment.py`): lowest open/capacity, then fewest open, then longest since last assignment,
  then user id; queue profile rows locked `FOR UPDATE` in id order. Nobody eligible -> unassigned and the
  managers are notified. Availability/deactivation never moves tickets; `/support/staff/{id}/reassign` does,
  recorded and notified.
- **A branch with nobody on duty sends tickets to Head Office (2026-11-16)**: no active profile in the branch queue
  (other than the creator; away/busy/full still count as staffed) -> on confirm, reopen or while waiting
  (`assign_waiting`), the ticket moves to Head Office Support (`_past_empty_branch`, an automatic forward entry, no
  "forwarded by" banner) and is assigned there. Forwarding to such a branch is refused (`QUEUE_UNSTAFFED`) and not
  offered. Tests that need tickets to stay in a staffless branch use the `staffed` fixture (an away member).
- **Only a ticket's own queue moves it (2026-11-16)** (`TicketService._moves_from_here`): its staff, the manager of
  that level (`_home_level`: staff profile, else Org Admin = Head Office, Branch Manager = branch, Master Admin =
  platform), or its assignee. The Org Admin oversees branch queues: he works a branch ticket (reply, status,
  priority, assign, cancel) but has no "Move this ticket" and `/forward` is 403 `OVERSIGHT_ONLY`; the branch
  escalates it. Each destination carries `how` (`_how`): `escalate` branch -> Head Office, `send` to Technical
  Support, `hand` down/back to the bank (`bring`, into the mover's own queue, is no longer reachable from oversight).
  The forward entry stores it (`data.how`); buttons, dialog, timeline, banner and the "Moved to you" notice use it.
- **Never by hand for oneself (2026-11-16)**: there is no "take"; `/assign` to yourself is 403 `SELF_ASSIGN` (a
  manager or lead may still move a ticket to someone else in the queue). Waiting (unassigned) tickets go out by
  workload, oldest first, whenever room may appear (`TicketService.assign_waiting`): staff added, updated or back
  to available, a ticket resolved, cancelled or forwarded away; and every SLA sweep (`assign_waiting_everywhere`).
- **Forwarding (2026-11-06)**: whoever forwards or escalates a ticket keeps seeing it (`queues.forwarded_by_clause`, an
  EXISTS on their forward entry, no new column), **read-only** once it is in a queue they do not work
  (`TicketService._forwarded_away`; `read_only` on the ticket; reply/note/attach refused with 403
  `TICKET_FORWARDED_AWAY`; their own box `forwarded`, "Forwarded by me"). The creator and a ticket forwarded back
  to the person's queue are never read-only. The new team sees `forwarded` (latest forward: who, role, from
  queue, reason) as a banner on the ticket and in the list; the new assignee's notification says "Forwarded to
  you" with who, from where and why.
- **The creator never handles their own ticket (2026-11-06)**, even as a member or manager of its queue
  (`queues.can_handle`, `handling_clause`; assignment already skipped them; a manager assigning it to them gets
  `ASSIGNEE_IS_CREATOR`). The creator's only status change is Reopen, on a resolved or closed ticket
  (`CREATOR_TRANSITIONS`); "Accept and close" is gone, resolved tickets auto-close (`SUPPORT_AUTO_CLOSE_DAYS`).
- **Withdrawn tickets (2026-11-16)**: "My tickets" lists every status by default (filters: Resolved, Closed, Withdrawn
  or cancelled). The creator may reopen a ticket they withdrew themselves (`_withdrawn_by_creator`: the last
  cancelled status entry is theirs), never one a manager cancelled. A withdrawn draft returns to
  `awaiting_confirmation`; a sent one reopens (`reopen_count`+1) with fresh due times (`sla.restart`) and is
  assigned like any reopen.
- **No messages on a resolved ticket** (`NO_MESSAGES` = resolved, closed, cancelled): reply, internal note and attach
  are refused (409 `INVALID_STATE`) and hidden. The creator reopens it, or the team moves it back to in progress.
- **Timeline** is `ticket_messages` with `kind` reply/note/status/forward/assign. Internal notes carry the level
  they were written at and are shown only to that level's staff side (`reads_notes_of`), never the creator.
- **SLA** (`sla.py`, settings `SUPPORT_SLA_HOURS` etc.): due times from priority at confirmation; the
  `support.sla_sweep` job (every `SUPPORT_SWEEP_MINUTES`) warns/breaches once per due time (notification
  `dedupe_key`) and auto-closes resolved tickets after `SUPPORT_AUTO_CLOSE_DAYS`. A creator may escalate an
  overdue ticket.
- Tests: `app/tests/integration/test_support_center.py`.

## nginx deploy: port + domain (2026-11-16, `app/modules/nginx/service.py`, `deploy/README.md` 4a/4b)

- One click (Organizations > Nginx > Deploy, step-up code) writes `governix-<slug>.conf`: a server block on the bank's
  port, and with `NGINX_BASE_DOMAIN` one for `<slug>.<base>` on `NGINX_DOMAIN_PORT` (the Node backend's
  `DEFAULT_BASE_DOMAIN`). The live site is IP-based (http://89.116.122.215:<port>): leave NGINX_BASE_DOMAIN unset and
  set `NGINX_PUBLIC_HOST` so the page links each port. `nginx -t` + reload; any failure puts the previous file and link back.
- Ports by default (2026-11-16): a new organization without `allowed_port` gets the lowest free port of
  `TENANT_PORT_MIN..MAX` (7003-7100; 7001 is the Master Admin portal); `/organizations/options` returns `next_port`
  and the panel's Add Organization form fills it in. Keeping an existing port is never refused, even outside the range.
- `NGINX_ENABLED_DIR` (sites-available + sites-enabled, the Node layout): the file is linked there; Disable unlinks.
- `NGINX_CERTBOT_COMMAND` (`certbot certonly --nginx ...`): a domain without a certificate gets one
  (`--cert-name governix-<slug>`), then the file is rewritten with 443 + http→https redirect. A certbot failure leaves
  the site live on http and returns `warning` (shown on the page). The file stays deterministic (the template, not
  certbot, writes the ssl lines), so "outdated" detection still works.
- `GET /tenant-info?host=&port=` (public, `organizations/tenant.py`): the sign-in page's bank, by domain first, then
  port; active organizations only; name, colors, font, logo (`/tenant-info/logo`). The admin panel's TenantContext
  sends `host`.
- Not built yet: a custom domain per bank (the panel's "Add custom domain"). It needs an `organizations.domain`
  column: ask before adding it. Tests: `app/tests/integration/test_nginx_deploy.py`.

## Role dashboards (2026-11-16, `app/modules/dashboard/`, for the admin panel's three dashboards)

- `GET /dashboard/platform` (Master Admin only), `/dashboard/organization` (Org Admin only), `/dashboard/branch`
  (Branch Manager only); any other role gets 403 `WRONG_DASHBOARD`. `period=7d|30d|90d` sets the activity charts
  (7 daily points, 4 weekly, 3 monthly; UTC days). `GET /dashboard` (the old shared one) is unchanged.
- No schema change. Two things the UI shows are not recorded, so they are derived:
  - "Acknowledged / read" = the User opened one of the policy's documents (`document.accessed`/`document.viewed`)
    after the assignment was made (`service._opened_since_assigned`). A real "I have read it" would need a column.
  - Gateway traffic is not recorded; the Master Admin gets each organization's app activity instead (sign-ins, AI
    questions, documents opened, uploads, from the audit log).
- Scope: the Master Admin sees counts only, never an organization's documents; its support figures are the
  Technical Support (platform) queue. The Branch Manager has no audit permission, so its activity feed is built
  from tickets, documents, assignments and staff of the branch, not from the audit log.
- Organization status on the platform dashboard: `provisioning` = active but its port is not serving (no port,
  not deployed or disabled); with `NGINX_CONF_DIR` unset, an organization with a port counts as active.
- Field-by-field map to the UI: `docs/dashboards-api.md`. Tests: `app/tests/integration/test_role_dashboards.py`.

## Organization-wise AI management (2026-11-17, `app/modules/ai_management/`, `/organizations/{id}/ai`)

Six modules per organization, configured by the Master Admin only (admin panel: Organizations > an organization's AI
button > `/masteradmin/organization/:id/ai`; Org Admin read-only at `/organizationadmin/ai-usage`):
API key vault, models, question limit, token limits, credit budget, alerts. Tables in migration `a3f2e8b1c9d7`.

- **Every AI call of an organization is metered** (`runtime.py`): `organization_llm` / `organization_embedder` /
  `search_embedder` wrap the provider. Each call reserves an upper estimate in `org_ai_usage` under the
  organization's advisory lock (milliseconds, never held during the call), then settles with the provider's actual
  tokens and cost. So concurrent requests cannot overspend; a reservation never settled stops counting after 15 min.
  The output budget of a call is cut to what the limits leave (`MIN_OUTPUT_TOKENS` 256, else refused).
- `reserve_question` (in `/ai/ask` and `/ai/ask/stream`, before anything runs) charges one question and refuses with
  HTTP 429 (`QUESTION_LIMIT_REACHED`, `TOKEN_LIMIT_REACHED`, `CREDIT_LIMIT_REACHED`) or 409
  (`AI_MODEL_NOT_CONFIGURED`). A limit hit mid-answer is the no-answer reason `AI_LIMIT_REACHED` (transient, not cached).
- `AIBlocked` is an `AppError` **and** an `LLMUnavailableError`, so optional AI (naming, summaries, ticket triage)
  degrades as it does in an outage; embeddings raise `AIEmbeddingBlocked` (searches fall back to keywords, an upload
  stops with `AI_LIMIT_REACHED`).
- **Nothing configured = no change**: no models assigned and no key → the runtime's shared platform LLM/embedder
  (`fallback=`), metered but unlimited. Assigned models → only those (default first); all disabled → blocked.
- Periods are computed from the clock (`service.window`), so resets need no job. Credits: monthly (UTC) or one total
  budget; rollover carries the previous month's unspent budget, computed once per month (`carried_for`).
- Prices: the model's own price on its assignment, else `ai_usage.service.PRICES` / `OPENAI_PRICES`. With a budget
  enforced, an unpriced paid model is refused (`AI_PRICE_UNAVAILABLE`). The provider's own balance is shown only for an
  OpenAI admin key (Costs API) and never changes the internal budget.
- Keys: Fernet via `auth.mfa.encrypt` (same `MFA_ENCRYPTION_KEY`), shown as `key_hint` only; add/rotate/enable/disable
  need step-up. One active key per provider **and kind** (partial unique index). The panel offers OpenAI only for now
  (`service.ENABLED_PROVIDERS`; the API still accepts every integrated provider).
  - `kind="api"` makes the calls; checking it stores the chat/embedding models it may use (`details.models`), which
    the assign-model form offers.
  - `kind="admin"` (an OpenAI `sk-admin-` key, detected by prefix) never makes calls: it reads OpenAI's Costs and
    Usage APIs (`provider_account`, optionally for one `openai_project_id`, cached 10 min,
    `GET /ai/provider-account?refresh=true`). OpenAI has no API for the remaining credit, so that is never shown.
- Alerts (`evaluate_alerts`, after every call and in the support sweep): default thresholds 70/80/90/95/100 per
  resource; claiming a row in `org_ai_notif_sent` is the dedupe (once per threshold and period; a jump sends the
  highest only). Recipients: Master Admins (link to the AI page) and the organization's Org Admins.
- Tests: `app/tests/integration/test_ai_management.py`.

## Answer pipeline (`app/modules/rag/service.py`, `RAGService.answer_events`)

1. Standalone rewrite.
2. `plan_query`.
3. `retrieve_with_variants`: hybrid lanes (exact / keyword / vector / section).
4. `build_evidence` (`evidence.py`).
5. `_gate`: key-term coverage plus IDF-weighted salient coverage. Skipped when a passage's vector
   similarity is at least `RAG_SEMANTIC_MIN_SIMILARITY`.
6. `_refuse_if_ambiguous`.
7. LLM call (`prompts.SYSTEM_PROMPT`; JSON claims plus `insufficient_evidence`).
8. `validate_claims` (`validation.py`: figures, word support, subjects).
9. `_check_on_topic`.
10. Summary.

On a refusal the pipeline may restate or split the question (`query_rewrite.restated_questions`) or
fall back to earlier versions.

`evidence.key_terms()` decides which question words must appear in the evidence. It leaves out
`QUESTION_TERMS` (framing words), `CLOSED_CLASS_TERMS` and the reader's own figures. A word that
appears in no visible document gets the highest weight. That is how "FIU-IND" asked of a bank with
no KYC policy is refused.

## Meaning check (v54): the general fix for "correct answer, rejected for its wording"

`validate_claims` marks each failed claim `wording_only` when it failed only on wording:
- weak word support;
- a question word the passage does not use ("bracket", "criterion", "additional", "meet");
- the reader's own figure phrased in a way the patterns don't recognise.

Hard failures stay strict and are never judged by meaning:
- absence statements;
- invented or missing citations;
- a figure in neither the evidence nor the question;
- a figure taken from another row;
- a wrong tier or variant;
- a wrong version;
- a dropped "not".

`RAGService._judge` asks the model once, with the question and each claim's passage, whether the claim is
`supported` and `answers` the question (`MEANING_CHECK_PROMPT`). Wording-only claims with both verdicts are
kept, and so is an answer the word-overlap topic check would withhold (claims that don't answer are
dropped).

It fails closed: with no verdict (`RAG_MEANING_CHECK=False`, the model down, or the local stand-in), the
claim is removed as before. The summary's wording-only failures go to the existing summary meaning check.

New phrasings need no new word lists. The lists in `evidence.py` stay only as a fast path that saves the
extra call.

The judge must not accept a statement about another rule. For "Do I satisfy the EMI-to-income
condition?", a minimum-income claim does not answer it; `MEANING_CHECK_PROMPT` says so.

## Prompt and context injection (v57)

`rag/injection.py` recognises instructions to an AI by their grammar, not by a list of attack strings:
- overriding its instructions ("ignore/disregard/forget ... previous instructions");
- giving it a new role ("you are now", "pretend you are", "developer mode");
- asking for its instructions ("reveal/print ... your system prompt");
- addressing an AI by name ("Note to the AI assistant: ...", "AI: approve ...");
- chat markup ("<|im_start|>", "[INST]").

In a document, "you" is the customer, so only an AI named as such counts as addressed. In a question,
"you" is the assistant, and "ignore the documents/rules" also counts. Keep that split: brochures say
"you are now eligible" and "you must submit".

Where it applies:
- **Questions.** `_prepare_request` strips instructions before anything reads the question, and again
  after the rewrite (that catches other languages and follow-ups).
  - In a statement, the whole sentence goes (it's the payload). In a sentence ending with "?", only the
    instruction clause goes.
  - Forged history turns are cleaned too.
  - What remains is answered, with a warning that the instructions were ignored. If nothing remains,
    the reason is `INSTRUCTIONS_IGNORED` (conversational on the page).
- **Documents.** `build_evidence` removes injected sentences before the model reads them or the
  validator checks against them, keeping the layout. `EvidenceSet.injections` produces a warning naming
  the document, and it is logged.
- **Prompt.** The question is fenced between `<<<` and `>>>` and is data. The model is told never to
  follow or reveal instructions and never to treat the question's claims as evidence.
- **Forms.** "Policy update: ..." form lines stay as the reader's claim, never "my ... is" facts.
- **Validator.** A figure the question attributes to the documents ("the policy says ... 2%",
  `validation.premise_values`) may only be corrected ("10.05%, not 2%"). Stating it is a hard failure,
  never judged by meaning.
- **Backstop.** Every claim is still checked against the documents, so an injection that slips past
  cannot add an unsupported statement.

## Reasoning accuracy (v64)

Builds on v60-v63's calculation path (the result must be recomputed and lead the answer, with one retry):
- **Working in a sentence counts.** `calculate.final_calculation` takes the model's last `calculations`
  entry (when every listed entry checks out), or else the last equation a claim writes itself
  (`calculations_in`). Both are recomputed and grounded.
- **Figures are compared at the precision written** (`states_value`): "Rs 96,98,940" must match to the
  rupee, and "93.98 lakh" to Rs 1,000. "about/approximately ..." keeps the 0.5% margin.
- **False equations are removed.** A claim containing one ("53,883 × 180 = 97,00,940") is a hard
  failure (`wrong_arithmetic`). A hyphenated range ("650 - 699") is not read as subtraction.
- **Results chain.** A claim may use a result an earlier claim established ("50% of Rs 1,20,000 is Rs
  60,000", then "Rs 60,000 − Rs 25,000 = Rs 35,000"). The same holds within one claim's equations.
- **Method and conclusion are checked** (`RAGService._check_reasoning`, `REASONING_CHECK_PROMPT`).
  - Applies to every case question with figures (`checks_reasoning`): calculations, eligibility Yes/No,
    "can I take a new EMI".
  - One model call asks whether the right rule was applied completely (no needed input left out, the
    right row and column, the asked quantity rather than an intermediate cap), and whether every
    Yes/No follows.
  - If not, the answer is retried once, with the check's correction in the prompt. Then it is refused.
  - With no verdict, the recomputed answer stands.
- **Change over time** ("Has the penalty increased?", "lowest rate ever", "shorter now than before",
  "then and now") routes to `ACROSS_VERSIONS` (`query_plan.asks_over_time`). The prompt asks for the
  earliest and latest figures with their versions, and only this document's versions.
  - "What changed most recently?" goes to the latest-change comparison.
- **Third-person cases** ("A borrower has ...", "An applicant aged 27 ...") are the reader's case
  (`_ABOUT_READER`).
- **Compute call (v65).** gpt-4o-mini often answers a calculation question with only the rule ("The
  maximum FOIR is 50%"). When an attempt has no recomputable final figure (`_missing_calculation`), one
  short call (`_computed_answer`, `COMPUTE_PROMPT`) asks only for the expression and a result sentence.
  The result is validated, recomputed and reasoning-checked like any answer.
  - The result is stated once, preferring the claim that shows its working. No separate "Calculation:"
    line is added when a claim already shows it.
- Dates ("2019-08-15") are never key terms; they choose the version. "Q." / "Q63:" labels are dropped.
- **The result may come after its working (v68).** `calculate.states_result` also accepts a claim that states
  the recomputed result anywhere ("After subtracting Rs 65,000 from 60% of Rs 2,00,000, the maximum EMI is Rs
  55,000"), but only when the result is not one of the calculation's inputs (`Calculation.inputs`). An operand
  or an intermediate step ("allows Rs 1,20,000 of EMIs") still never counts as the answer.
- **Expressions written with their result (v69).** gpt-4o-mini often writes `"0.60 * 200000 - 65000 = 55000"`
  in `calculations`. `checked_calculations` splits on "="/"≈", recomputes each step (a chain "0.6 * 200000 =
  120000 - 65000 = 55000" works) and reads "60%", "x" and Indian grouping. A stated result must equal the
  recomputed one; a wrong one ("= 65000") is dropped, reported by `wrongly_stated`, told to the retry ("Your
  calculation gives ... = 55,000, not 65,000") and its claims are not reused by `_computed_answer`.
- **The model's arithmetic is never needed (v70).** gpt-4o-mini picks the right expression but often misstates
  its result ("Rs 35,000" or "Rs. 65,000" for 0.60 × 2,00,000 − 65,000). When every listed calculation checks out
  but no claim states the recomputed result, `calculate.restated_result` writes it in the question's words
  (`asked_quantity`: "what is the maximum permissible EMI?" → "The maximum permissible EMI is Rs 55,000: 0.60 ×
  2,00,000 − 65,000 = 55,000.") and drops the model's claims that give that quantity another figure.
  - Only for money: not for a tenure, period, count, rate or share (`_NOT_MONEY`), nor a division by another
    figure (a ratio). "Rs" only when the question uses a currency.
  - The method stays the reasoning check's to judge (an expression that forgets the obligations is a wrong
    method, caught there).
- **Every attempt is in the audit trail (v68).** A no-answer's `plan.checks` lists each written attempt
  ("Attempt 2: ..."): what validation removed, a failed reasoning check, `insufficient_evidence`, or "No claim
  states a recomputed result" with the calculations and claims the model wrote. Read `audit_events`
  (`action='ai.query'`) to see why a UI question was refused, instead of re-running it.

## Framing words, misspellings, document lists and overviews (v73)

The general fix for "a word that names nothing decided the answer" ("Tell me *something* about ...", "... *so far*?"):
no word lists.
- **Every question is read once by the rewrite** (`query_rewrite.standalone_question`), not only ones a pattern
  flags. For a question that looked complete, only spelling may change (`_spelling_only`: each changed word ≥0.75
  similar, or words run together split, e.g. "morgtage" → "mortgage", "documentsyou" → "documents you"). Any other
  rewording is discarded.
- **Framing words** (`Rewrite.framing`, the model's `framing_words`): words that only frame the ask. They are not
  required by the gate, the on-topic check or `_has_subject` (`RAGService._framing`).
  - A word that no visible document uses is framing only when it is a plain word as typed (v75,
    `service._plain_word`: letters only, lower case or opening a sentence, not hyphenated/dotted, and not before
    another subject word). "something" and "consumed" can frame; "FIU-IND", "NRE" and the "car" of "car loans"
    never do, so those stay refused. Typos are left to the restatement.
  - Figures are never framing. Every claim is still validated.
- **"Which documents do you have?"** (`kind == "documents"`, no policy named, and no remaining word a document
  uses) gets `DOCUMENT_LIST`: the policies the reader can see (same ACL as search), each with its version in force.
  No search, no model call for the answer. "What documents are needed for a home loan?" still searches.
- **Overview** (`plan.overview`, `_asks_overview`): a question that names a policy and nothing else ("tell me about
  the mortgage policy"). The search is limited to that policy, its opening passages and contents are read
  (`build_evidence(whole_document=)`), and the prompt asks for purpose and main topics. It skips the term gate, the
  ambiguity question and the on-topic check, and never falls back to an earlier version.
- Without a model (local stand-in, outage) there are no framing words or kinds (`Rewrite.kind` is None). The
  OpenAI client falls back to the local stand-in when a call fails, so the v74 rules below work without one.

## Meaning decides, not the words (v75-v76)

The user's rule: people ask in any words and any sentence shape. With a model, the model's reading of the question
decides; word patterns and word counts are only the fallback for when no model answers (outage, local stand-in).
Don't add patterns or word lists to route or refuse questions; extend the model's reading instead.

- **Question type by meaning.** The rewrite returns `time_scope` ("changes" / "since" / "compare" / "current").
  Whenever it is set, `plan_query` follows it (`_by_meaning`): "Did the fee go up?", "How did the LTV limit
  evolve?", "Which edition was cheapest?" → `ACROSS_VERSIONS`; "Which version brought in X?" → `since`; "What is new
  in the latest policy?" → `COMPARISON`. What the question states as data still counts: the UI's choice, version
  labels and dates. Without a model (None) the old patterns decide. The rewrite also lists words that only ask how
  something moved ("go", "up", "evolve", "always") as framing.
- **Refusing on word counts needs the model's agreement.** When `_gate` finds too few of the question's words in
  the passages (coverage or salient coverage), `_covers_by_meaning` asks the model (`RELEVANCE_CHECK_PROMPT`)
  whether a passage addresses the question by meaning. Yes → answered (`evidence.meaning_match`, which the
  on-topic check treats like a close vector match); every claim is still validated and meaning-checked. No, no
  verdict, or the stand-in → `KEY_TERMS_NOT_FOUND` as before. At most `MAX_RELEVANCE_CHECKS` (3) per question,
  since a refusal also tries earlier versions.
- **Small talk in any words** (`Rewrite.kind == "chat"`: "kem cho", "I am fine", "how are youuu") gets the
  greeting reply, not "not covered". `is_greeting` stays for when no model read it.
- **Restating a refused question in the documents' words.** After a refusal in `CLARIFY_REASONS`, `_clarify`
  shows the restatement the nearest passages of that search (`_nearest_passages`: "heading: start of text", 12
  at most). It may add the documents' term in brackets ("Can I close my home loan early (foreclosure)?"), never
  swap the subject. `_keeps_subject` checks that every question word the documents use is still there; if not,
  the restatement is asked again without the passages (the old behaviour).
- Re-embed any organization whose chunks have `local-hashing-v1-1536` vectors: for those documents only the word
  lanes work, and none of the meaning paths above (vector gate bypass) can help.

## Words that name nothing, with or without a model (v74)

- **Closed classes, never a subject** (`evidence.CLOSED_CLASS_TERMS`): interjections and discourse markers ("okay",
  "ok", "hmm", "yeah"), informal function words ("kinda", "sorta", "wanna", "pls", "abt") and "here". The model can't
  mark them as framing: no document uses them, and `_framing` keeps every word no document uses (the FIU-IND rule).
  - A classifier before "of" ("sort/variety/forms/class of") frames the noun after it (`_CLASSIFIER_OF`).
  - An opening "okay then / so / now / well / hmm" is set aside before anything reads the question
    (`query_rewrite.without_opener`). Not "so far", "now that", "well-known", or a message that is nothing else.
- **Which documents the reader holds** (`evidence.documents_held`): "which is the documents assign on me", "policies
  available for me", "what documents do you have", "which policies can I ask about", "my documents". The words that
  tie the documents to the reader or the assistant are the relation, whatever the verb or its spelling, so they are
  dropped before the remaining-words check. This is used with the model's `kind == "documents"` too: the model never
  lists an action as framing, and "assign" is a word documents use.
  - Documents a customer hands in ("need", "submit", "mandatory", ...) are never the catalogue.
- **The documents named only by a common noun** ("What is the policy?", "what is the documents", "what kind of the
  policy", "summarize the policy"; `_names_documents_only`):
  - If the reader can see exactly one policy (or chose policies in the UI), it gets an overview of that policy.
  - Otherwise it gets `DOCUMENT_LIST` asking which policy they mean.
  - Not for versions, clauses, the reader's case, or documents a customer hands in.
- **"What documents do I need?"** has a subject (`asks_for_submitted_documents` in `_has_subject`) and is searched,
  not `NO_SUBJECT`.

## "Not found" answers (v58)

For the reasons in `NOT_FOUND_REASONS`, `service._explain_not_found` builds the message from what was read:
the nearest section headings and documents in the evidence. "Front matter" and contents pages are left out.

- Not covered (`KEY_TERMS_NOT_FOUND` / `LOW_RELEVANCE` / `NO_RELEVANT_DOCUMENTS`): "Your documents don't
  seem to cover this. The nearest sections I found, “Pricing and Fee Schedule” ... in the Home Loan
  Guide, are about other topics."
- Not stated (`INSUFFICIENT_EVIDENCE`): "I read ... but they don't state this, and I won't guess."
- Not quotable (`ANSWER_FAILED_VALIDATION`): "... nothing there states the answer clearly enough for me
  to quote it."
- Off topic (`ANSWER_OFF_TOPIC`): "The closest passages ... are about something other than what you
  asked."
- Claim not confirmed (v66, `CLAIM_NOT_CONFIRMED`): any of the reasons above, for a question that asks to
  confirm what someone says (`evidence.asks_to_confirm`): "I can't confirm that claim. I read “Data privacy
  and record keeping” in the Mortgage Loan Policy, and nothing there says it." Heading: "Not confirmed by
  your documents".

- Users (v71-v72): a User reads only the policies assigned to them (`auth/acl.py`).
  - `NOT_ASSIGNED` ("ask your Branch Manager to assign it") replaces a not-found reason only when words of the
    question that the User's own policies lack all occur in an active, unassigned policy their scope reaches
    (`RAGService._assigned_elsewhere`, word counts only). The policy is never named or quoted.
  - Otherwise the ordinary not-found message stands, so a general question is not called "not assigned".
  - A User with nothing assigned gets `NO_ASSIGNED_POLICIES` before any search or model call, unless the message
    is a greeting or has no subject (those keep their conversational replies).

Suggestions are questions about those sections ("What does the Home Loan Guide say in “Who Can Apply”?").
When nothing came close, the generic tips in `SUGGESTIONS` are used.

The page heading depends on the reason (`NOT_FOUND_HEADINGS` in `AssistantPage.tsx`): "Not covered in
your documents" or "Your documents don't answer this directly", never "I couldn't find this". Suggestions
come from ACL-filtered evidence, so they never name a document the reader can't see.

## Clarifying questions (v56)

- **When to ask "Which one do you mean?"** `_refuse_if_ambiguous` asks only when at least 3 matching rules
  are variants of one rule (`_variants_of_one_rule`: ≥50% shared words, `VARIANT_OVERLAP`) and set
  different figures. Example: "approval note retained ... 7 / 10 / 14 years".
  - Rules that only share the question's word ("What will my EMI be?" matches Flexi-EMI, EMI dates, the
    EMI cap and the overdue penalty) go to the model.
- **Wording.** The message names the policy once ("Home Loan Guide (Version 8)"), and the choices are
  readable rules with the bullet removed, cut at a word. The page adds the "Which one do you mean?"
  heading and the "Matching rules" list, so the backend repeats neither.
- **Questions missing a detail** ("What will my EMI be?" with no amount or tenure). The prompt says to
  give what the figure depends on and an example row, then ask for the missing details. That is an
  answer, not `insufficient_evidence`.

## Input forms and arithmetic (v55)

- **Filled-in forms.** `query_rewrite.from_form` runs first in `_prepare_request`. A message such as
  "Context: / Age: 35 / Income: ₹60,000 / Version: 6 / Question: Do I ...?" becomes "My age is 35 and my
  income is ₹60,000. Do I ... in Version 6?", so the reader's-case handling and version routing apply.
  - "Expected:" and "Answer:" blocks from pasted test cases are dropped, and short headings ("Q63")
    are skipped.
  - Ordinary multi-line questions are unchanged.
- **Version labels.** `_VERSION_REF` also accepts "Version: 6".
- **Arithmetic written in a claim (v59, the whole "how much" family).** The model usually writes the
  working inline, as people do: "Rs. 1,07,767 × 180 months − Rs. 1 crore = Rs. 93,98,060", "(Rs. 53,883 ×
  180) − (Rs. 1,06,358 × 60) = Rs. 33,17,460", "50% × Rs. 60,000 − Rs. 10,000 = Rs. 20,000".
  - `calculate.written_arithmetic` tokenises figures with currency marks, units (months/years), scales
    (lakh/crore/thousand/k) and %, and operators as symbols or words (minus/less/times/divided by). It
    accepts "=", "≈", "is" and "about".
  - Every figure must be grounded (evidence, question, or a unit conversion), and the stated result must
    match the recomputed one.
  - The prompt asks for the working inside the claim, and lists the family's formulas: total repaid,
    interest paid, saving, share, room for new EMIs, fee on an amount, and maximum loan.
  - `_derived` also accepts EMI × months, using the question's years in months.
  - For a claim whose figure was verified this way, the question's own words and words addressing the
    reader ("you would pay", "you can borrow") count as supported.
  - Wrong or ungrounded arithmetic stays a hard failure.
- **Shares.** `_derived` also accepts one figure as a percentage of another ("Rs. 35,000 is 58.33% of
  Rs. 60,000").

## Question types (since 2026-10-07)

- **Conditional / the reader's own case** ("If my credit score is 680...", "I am 25, can I apply?",
  "I'm a software engineer earning 70k..."):
  - `evidence.readers_situation()` splits off the situation clause. Numbers in it are the reader's
    figures and are not required in the evidence.
  - Situation words that no visible document uses (`RAGService._circumstances`) are skipped by the gate
    and by the on-topic check, but only when the question also names a subject of its own. Claims are
    still checked against them.
  - The ambiguity refusal is skipped, because the reader's case picks the row.
  - A statement about the reader runs on until a clause asks something, so "I am 27, earn Rs 65,000 and
    have a 760 score. Can I get a loan?" is all situation up to "Can I".
  - The prompt says to check every figure in its own claim and give the overall Yes/No in the summary.
    `_unchecked_figures` is the safety net: if some figures are checked but others are not, it adds a
    warning and drops the summary.
  - The validator accepts the reader's outcome ("You are 27, which is below ...", "at 27 you do not
    qualify"): a claim that applies the reader's figure may add a negation (`_flips_polarity(outcome=)`),
    but never drop the rule's own one. "27 years" counts as the reader's "27".
  - Earlier-version notes for reader-case questions match each rule by its own wording
    (`_rule_worded_like`, `SAME_RULE_OVERLAP`), failing rules first. Example: v8 age 28-65, v7 25-65.
  - `evidence.READER_OUTCOME_WORDS` ("meet", "satisfy", "criterion", "criteria", "eligible", "eligibility",
    "qualify", "requirement", "stated") are framing only when `applies_to_reader(question)`. They are the
    yes/no frame of "do I meet the minimum-income criterion?". Without a reader's case they stay gate terms
    ("What are the eligibility criteria?"), as v49 intended.
  - Don't make these words required again for reader-case questions. A correct claim ("Rs. 49,000 is
    below the Rs. 60,000 minimum") is then withheld as off topic because it doesn't say "meet", and a
    claim saying "criterion" fails the subject check.
  - (v67) "threshold(s)", "cut-off" and "pass(es/ed)" are reader outcome words too ("Do these stated
    thresholds pass?"). "Failed" and "cleared" are not: a failed transaction or a cleared cheque is a subject.
  - (v67) In a third-person case the noun for the reader ("a borrower has ...", "an applicant aged 27",
    `evidence.readers_names`) is framing like "I": the policy may say "applicant", and "the borrower's score"
    is the reader's figure, as "your score" is.
  - (v67) The reader's duration may be restated as a bare number ("is 22 years old" → "an age of 22"), as a
    bare number may gain its unit ("I am 27" → "27 years").
  - (v67) A claim that puts several of the reader's figures in one sentence cannot be tied to its rows
    (`_unbound_numbers`, a hard failure). The prompt forbids it; the retry is told what removed each claim
    (`_reasoning_feedback` now also carries validation problems) and to write one claim per figure.
  - (v67) A figure equal to a minimum or maximum meets it. A rule the question gives no figure for (age at
    maturity without a tenure) is "not checked", never met or failed; the reasoning check does not count it
    as a missing input.
  - (v67) A withheld answer's checks are kept in the audit trail (`plan.checks` of the no-answer), so a UI
    failure can be diagnosed from `audit_events` without re-running it.
  - "additional", "extra", "further" and "else" are additive framing (in `QUESTION_TERMS`). The word they
    qualify stays a term.
  - A yes/no question ("Is/Can/Does/Am ...", `evidence.asks_yes_no`) that gives a bare figure ("Is age 25
    eligible?", "Is age 24?") tests that figure, so it counts as the reader's figure
    (`situation_figures`). Years are excluded, because they choose versions.
  - The prompt requires a "Yes"/"No" first claim that sets the figure against the rule, and a summary
    that starts the same way. `_unchecked_figures` flags a yes/no reply that gives the rule without
    applying the figure.
  - Placing a figure in a band ("Is credit score 650 in the 650-699 bracket?", "Which band does 680 fall
    in?", `evidence._PLACES_FIGURE`): "bracket", "band", "slab", "tier", "range" and "falls in" are the
    reader's words for a table row (`READER_OUTCOME_WORDS`, and `validation._FRAMING` for support). Tables
    list just "650 - 699".
  - If "bracket" were a required term, the v8 "Yes" claim would be removed and the answer would fall
    back to an earlier version without saying Yes.
  - A figure said to be the reader's in a claim ("Your total EMIs are 60%") is accepted beside a rule
    figure (`validation._READERS_OWN`). Arithmetic on the reader's own figures (60% of Rs. 50,000) must
    go through `calculations`.
- **Advisory and speed** ("should I", "which is better", "how fast", "turnaround"): these words are in
  `QUESTION_TERMS`. The prompt says to give the options and trade-offs the documents state, with no
  verdict.
- **Process and service** ("how do I...", "how long", "online?"): handled by prompt rules.
- **When did it start or change** ("When did the 50% EMI-to-income rule start?", "Since when is
  Flexi-EMI offered?", "Which version first introduced X?"):
  - `query_plan.asks_when_introduced()` routes the question to `ACROSS_VERSIONS` with `plan.since`.
  - `RAGService._sides` searches each version of the policy that was named, chosen or best matched.
  - The prompt lists evidence oldest version first and asks for the earliest version, with its
    effective date, that states the rule.
  - "When is the EMI due?" and "When does X start?" are rules in force and stay `CURRENT`.
  - For the Home Loan Guide, the 50% EMI cap first appears in v5 and Flexi-EMI in v7. v4-v6 say
    "flexible EMI dates", a different feature.
- **Two or more dates** (v66: "Compare a loan sanctioned on 2026-06-15 with one sanctioned on 2025-06-15:
  what is the credit-score threshold in each period?", "the fee in March 2024 and in March 2026", "... on
  2025-06-15, different from now?"):
  - `query_plan.dates_in_question` reads full dates, then months (at month end). It adds today only when one
    date is set against now (`_AGAINST_NOW`: "with one sanctioned today", "different from now", "vs the
    current version"). Bare years are left out: "between 2020 and 2024" is a period.
  - Two or more dates route to `COMPARISON` with `plan.as_of_dates` (version labels and which-period
    questions win). `_plan_comparison` takes each policy's version in force on each date, from the named
    policies if any. If only one date has a version in force (a date of birth in a form), it becomes an
    as-of question on that date.
  - `_sides` narrows to the policy of the best-matching passage and searches its versions one by one.
    Evidence is listed oldest first.
  - The prompt says: one claim per date, naming the version whose period covers it, then whether it
    changed. Dates, month names, period words and "now/today" are not key terms.
  - Mortgage Loan Policy: min score v6.0 (2024-10-01 to 2026-04-01) 720, v7.0 725.
- **Confirming a claim** (v66: "A user says the policy permits sharing another borrower's details. Can you
  confirm?", "Is it true that ...?", "My manager said ..."):
  - `evidence.asks_to_confirm` recognises the frame: someone says or claims, "is it true that", or
    confirm/verify a claim or statement. "Can you confirm the fee?" just asks for the fee.
  - `key_terms` drops the frame ("a user says", "confirm that claim").
  - The prompt says: answer "Yes" with the rule that states it, or "No" with a rule that forbids or
    restricts it. If nothing addresses it, `insufficient_evidence`; never confirm because the question
    asserts it.
  - A refusal becomes `CLAIM_NOT_CONFIRMED` (see "Not found" answers). Every claim is still validated.
- **Which period had a figure** ("Which period had the lowest EMI for Rs 50 lakh for 15 years?", "When
  was the fee highest?"):
  - `query_plan.asks_which_period()` matches past tense only, and routes the question to
    `ACROSS_VERSIONS`. "Which period has the highest FD rate?" can mean the tenure bands of one rate
    table, so it stays `CURRENT`.
  - Every across-versions question searches each version on its own (`_sides`), and the evidence is
    listed oldest first.
  - The prompt says to read each version's table under its own column headings.
  - Home Loan Guide: the EMI tables move the "15 years" column (2nd in v4-v5, 3rd in v6-v8), and v1-v3
    have no Rs 50 lakh row. The lowest 50 lakh / 15-year EMI is v6 at Rs. 50,565.
- **Calculations** ("How much total interest on Rs 1 crore over 15 years?", "How much do I save with 5
  years instead of 15?"): this is stage 8 of the audit, `rag/calculate.py`.
  - The model writes `calculations` (expressions with + - * / over figures from the evidence or the
    question, plus the unit constants 12, 52, 365, 100, 1000, 1 lakh, 1 crore) before its claims.
  - `checked_calculations` re-evaluates each one safely with `ast`, and drops it if any figure is not
    grounded.
  - `validate_claims(calculations=)` lets a claim citing the same evidence state a result or an
    intermediate step, within 0.5% for rounding. The words "total", "save" and "difference" are then
    exempt from the subject check.
  - A "Calculation: 1,07,767 × (15 × 12) − 1,00,00,000 = 93,98,060" claim is appended for each result
    used.
  - The claim stream reads the calculations from the JSON before the claims (`ClaimStream.preamble`).
  - Powers are not allowed, so an EMI is never computed from a rate. EMI scales with the amount
    borrowed, which is allowed.
- **Paraphrase** ("CIBIL" for credit score, "complaint" for grievance): a close vector match bypasses
  the gate. With close evidence, the on-topic check also ignores words that no document uses.
- **Known risk:** "If I take a car loan, what is the rate?" in an org with no car-loan document now
  relies on the model's `insufficient_evidence` judgement, because "car" is a situation word that no
  document uses.
