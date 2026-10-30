# Input validation

The rules every form and API request goes through: passwords, email, names, codes, files and limits. The
backend is the authority. The frontend repeats some rules (`required`, `minLength`, `pattern`) only so the user
sees a problem before submitting.

For the checks on AI answers, see `docs/validation.md`.

## How a rejected request looks

Every error uses the standard envelope. `error.code` is the HTTP status and `error.reason` the stable identifier.
The frontend's `ApiError.code` holds the reason.

**Schema failures** (Pydantic, `app/core/error_handlers.py`) return 422 with reason `VALIDATION_ERROR`, plus
one entry per field. Submitted values are never echoed back:

```json
{
  "success": false,
  "error": {
    "code": 422,
    "reason": "VALIDATION_ERROR",
    "message": "Request validation failed.",
    "details": [{ "field": "body.new_password", "message": "String should have at least 12 characters" }]
  }
}
```

**Business-rule failures** (raised in services) use their own reason, e.g. `EMAIL_TAKEN`, `INVALID_RESET_TOKEN`,
`UNSUPPORTED_FILE_TYPE`.

## Passwords

### Rules

| Rule | Value | Where |
|---|---|---|
| Minimum length | **12 characters** | `MIN_PASSWORD_LENGTH`, `app/core/security.py` |
| Maximum length | 1024 characters | Every password field in the schemas |
| Complexity (upper/lower/digit/symbol) | **None**. Length is the only strength rule | |
| New must differ from current | Yes, for change password and reset link | `AuthService.change_password`, `reset_password` |
| Storage | Argon2 (`argon2-cffi` defaults), rehashed at login when the parameters change | `hash_password`, `password_needs_rehash` |
| Temporary password | 16 random URL-safe characters (`secrets.token_urlsafe(12)`), shown once, never stored in clear | `generate_temporary_password` |

The login form checks only that the password is not empty (`min_length=1`). That way an account created
before the 12-character rule can still sign in.

### Where a password is set

| Endpoint / form | Schema | Password field | Notes |
|---|---|---|---|
| `POST /auth/login` | `LoginRequest` | `password`: 1-1024 | Email must be a valid address |
| `POST /auth/change-password` | `ChangePasswordRequest` | `current_password`: 1-1024, `new_password`: 12-1024 | Wrong current password: `INVALID_CREDENTIALS` ("Current password is incorrect."). Same as current: "New password must differ from the current password." |
| `POST /auth/forgot-password` | `ForgotPasswordRequest` | none | Valid email only. See [Forgot password](#forgot-password-and-reset-link) |
| `POST /auth/reset-password` | `ResetPasswordRequest` | `token`: 1-2048, `new_password`: 12-1024 | Frontend also requires the confirm field to match |
| `POST /users` (admin creates user) | `UserCreate` | `password`: optional, 12-1024 | Left out: the server generates a temporary password and returns it once |
| `POST /users/{id}/reset-password` (admin) | `PasswordReset` | `new_password`: optional, 12-1024 | Left out: generated. Ends all the user's sessions (`token_version += 1`) |
| `POST /organizations` (first admin) | `FirstAdmin` | `password`: 12-1024 | |

### Frontend checks

| Screen | Check |
|---|---|
| Login (`LoginPage.tsx`) | `required` email (type=email) and password |
| Change password (`UserMenu.tsx`) | `minLength={12}`, hint "At least 12 characters" |
| Reset password (`PasswordResetPages.tsx`) | `MIN_PASSWORD = 12` and the confirm field must match before the button enables |
| Admin create / reset user (`UsersPage.tsx`) | `minLength={12}`, hint "Minimum 12 characters" |

## Login and sessions

| Rule | Value | Setting |
|---|---|---|
| Email match | Case-insensitive; deleted users never match | |
| Failed attempts before lockout | 5 | `LOGIN_MAX_FAILED_ATTEMPTS` |
| Lockout | 15 minutes, then the counter starts again | `LOGIN_LOCKOUT_MINUTES` |
| Access token (cookie) | 15 minutes | `ACCESS_TOKEN_TTL_MINUTES` |
| Refresh token (cookie) | 7 days, rotated on every refresh, stored hashed | `REFRESH_TOKEN_TTL_DAYS` |

| Situation | Reason | Message |
|---|---|---|
| Wrong email or password | `INVALID_CREDENTIALS` | "Invalid email or password." |
| Inactive user | `INVALID_CREDENTIALS` | Same message, so it doesn't reveal the account exists |
| Locked | `ACCOUNT_LOCKED` | "Account temporarily locked after repeated failed logins." |
| Organization suspended | `ORGANIZATION_SUSPENDED` | "Organization is not active." |
| Expired / bad access token | `TOKEN_EXPIRED` / `INVALID_TOKEN` | |
| Another tab just refreshed | `TOKEN_ROTATED` | Within 30 s of a rotation; the family is not revoked |

To avoid revealing which emails exist:
- An unknown email is still checked against a dummy hash, so it takes the same time as a known one.
- A locked account still runs a hash before it answers.

A successful login resets the failure counter and the lockout.

**CSRF.** Every POST/PUT/PATCH/DELETE must send `X-CSRF-Token` equal to the `csrf` claim of the access JWT.
Refresh and logout compare it with the `governix_csrf` cookie. If it is missing or wrong: 403 `CSRF_FAILED`
("Missing or invalid CSRF token. Reload the page and try again."). The frontend client adds the header.

## Forgot password and reset link

- `POST /auth/forgot-password` always answers "If an account uses that email, we have sent it a link to reset the
  password." It never says whether the email is registered. No link is sent for deactivated or deleted accounts,
  or users of a suspended organization.
- Rate limit: **3 requests per email** and **10 per client** every **15 minutes**. Over the limit: "Too many
  password reset requests. Please wait a few minutes and try again."
- The link is `PASSWORD_RESET_URL?token=...`, valid for **30 minutes** (`PASSWORD_RESET_TTL_MINUTES`).
- The token is a signed JWT (type `password_reset`), and no row is stored. It is single-use because it carries a
  fingerprint of the current password hash and the user's `token_version`. Setting the password, or any session
  reset, spends it.

| Situation | Reason | Message |
|---|---|---|
| Expired link | `RESET_TOKEN_EXPIRED` | "This reset link has expired. Ask for a new one." |
| Bad, used or replaced link | `INVALID_RESET_TOKEN` | "This reset link is not valid or has already been used. Ask for a new one." |
| Same as current password | `VALIDATION_ERROR` | "New password must differ from the current password." |

A successful reset ends every session and clears any lockout. The page asks the user to sign in again.

## Users

`UserCreate` / `UserUpdate` (`app/modules/users/schema.py`):

| Field | Rule |
|---|---|
| `email` | Valid email (`EmailStr`), unique ignoring case. Taken: `EMAIL_TAKEN` "A user with this email already exists." |
| `full_name` | 2-200 characters |
| `phone` | Optional, at most 50 characters of digits and `+ ( ) - .` and spaces. Blank becomes empty. Frontend: `type="tel"`, same pattern |
| `role` | One of the `Role` values, and only a role the caller may assign. Otherwise 403 "You cannot assign the role X." |
| `organization_id` | Master admin only. Required when they create a user |
| `branch_id` | Must belong to the organization ("A valid branch_id in this organization is required.") |
| `department_id` | Must belong to the selected branch |
| `new_branch` | A Branch Manager's branch created with them. Branch rules below |

Service rules:
- A user can't deactivate themselves, change their own role, or delete themselves.
- Only the Master Admin moves a user to another organization, and only an account that never signed in.
- Deactivating, changing role or organization, resetting the password and deleting all end the user's sessions.

## Organizations

`app/modules/organizations/schema.py`:

| Field | Rule |
|---|---|
| `name` | 2-200 characters |
| `slug` | `^[a-z0-9][a-z0-9-]{1,98}[a-z0-9]$`: 3-100 lowercase letters, digits and hyphens, not starting or ending with a hyphen |
| `primary_color`, `secondary_color` | `#RRGGBB` hex |
| `brand_font` | One of the listed Google Fonts (`BRAND_FONTS`): "Choose one of the listed Google Fonts." |
| `contact_email` | Valid email |
| `contact_phone` | At most 50 characters |
| `website` | At most 300 characters |
| `address` | At most 2000 characters |
| Port | Between `TENANT_PORT_MIN` and `TENANT_PORT_MAX` (7001-7100): `PORT_OUT_OF_RANGE`. Used by another org: `PORT_TAKEN` |
| First admin | `email`, `full_name` 2-200, `password` 12-1024. The email must be unused: `EMAIL_TAKEN` |
| Logo | PNG, JPEG or WebP by extension **and** file signature, at most **2 MB**. Otherwise `UNSUPPORTED_FILE_TYPE` / 413 |

## Branches and departments

| Field | Rule |
|---|---|
| Branch `name` | 2-200 characters |
| Branch `code`, department `code` | `^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$`: 1-50 letters, digits, `_` or `-`, starting with a letter or digit |
| `street_address` | 2-300 characters |
| `state` | 2-100 characters |
| `postal_code` | `^[A-Za-z0-9][A-Za-z0-9 -]{1,18}[A-Za-z0-9]$`: 3-20 characters ("400001", "94105-1234", "SW1A 1AA"). Same pattern in `BranchFields.tsx` |
| Department `name` | 2-200 characters |

## Categories and policies

| Field | Rule |
|---|---|
| Category `name` | 2-100 characters after collapsing whitespace. Unique per organization ignoring case: `DUPLICATE_CATEGORY` |
| Category `slug` | `^[a-z0-9][a-z0-9-]{0,98}[a-z0-9]$`, unique |
| Category `description` | At most 2000 characters |
| `authority_rank` | 0-1000 |
| `keywords` | At most 100 |
| Policy `name` | 2-500 characters |
| Policy `scope` | `global` or `branch` |
| Policy `description` | At most 5000 characters |
| `policy_number` | At most 100 characters |
| `owner`, `issuer`, `issuing_department` | At most 200 characters |
| Policy `status` (update) | `active` or `archived` |
| Withdraw `reason` | 5-2000 characters (frontend disables the button under 5) |
| Version order | 1-1000 version ids |
| Move a document | `after_id` or `before_id`, not both |
| Relationship | At most 100 clauses, evidence at most 2000 characters |

## Documents and uploads

| Rule | Value |
|---|---|
| File type | PDF only: the name must end in `.pdf` **and** the first 1 KB must contain `%PDF-`. Otherwise `UNSUPPORTED_FILE_TYPE` ("Only PDF documents are supported." / "The file is not a valid PDF.") |
| Maximum size | `MAX_UPLOAD_SIZE_MB` = **1024 MB**. nginx's `client_max_body_size` is set 1 MB higher |
| Empty file | "The file is empty." |
| File name | `/` and `\` replaced by `_`, cut to 500 characters |
| Document `title` | 1-500 characters |
| Duplicate check | 1-500 SHA-256 hashes per request |
| Bulk upload | At most 200 groups, 500 files in all, 100 files per group. A group either adds versions to an existing policy or creates a new one, not both |
| Item | `filename` 1-500, `version_label` at most 50, new policy name at most 500 |
| Archived policy | `INVALID_STATE` "The policy is archived." |

**Ticket attachments.** PDF, PNG, JPG/JPEG, TXT, CSV, DOCX or XLSX by extension, at most **10 MB**, not empty.
The name is sanitised and cut to 300 characters.

## Assistant and search

| Field | Rule |
|---|---|
| `question` (ask) | 2-2000 characters |
| `history` | At most 10 turns. Each turn's question is cut to 2000 characters and its answer to 8000 (cut, not rejected) |
| `mode` | `auto`, `current`, `historical`, `version`, `compare` |
| `version_ids` / `policy_ids` / `category_ids` | At most 2 / 20 / 20 |
| Conversation `title` | 1-200 characters |
| Search `query` | 1-1000 characters |
| Search `limit` | 1-100 (default 20) |
| Search filters | At most 20 version ids, and 50 each of category, policy and document ids |
| Rate limit (`/ai/ask`) | 20 requests per 60 s per process (`RAG_RATE_LIMIT_*`). It can't be turned off in production |

Questions are also cleaned of instructions addressed to an AI before they are answered (`rag/injection.py`, see
AGENTS.md "Prompt and context injection").

## Changing a rule

- Change the backend schema or service first. A frontend check is only a convenience, and the backend must hold
  the rule on its own.
- Keep the frontend copies in step:
  - password length: `MIN_PASSWORD` in `PasswordResetPages.tsx`, `minLength={12}` in `UserMenu.tsx` and
    `UsersPage.tsx`;
  - `POSTAL_CODE` in `BranchFields.tsx`;
  - `PHONE_PATTERN` in `AdminUsersPage.tsx`;
  - `SLUG` in `OrganizationForm.tsx`.
- Raising `MIN_PASSWORD_LENGTH` doesn't lock anyone out (login only needs a non-empty password). The new length
  applies the next time each user sets a password.
- Ask before changing the DB schema (e.g. column lengths).
