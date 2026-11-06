# Deploying Governix

This guide sets up Governix on one Linux server (Ubuntu 24.04 LTS), from an empty machine to a running site, and
then covers updates, backups and checks. Run the commands as a user with `sudo`. The app itself runs as its own
user, `governix`.

What runs where:

| Part | What it is | Runs as |
| --- | --- | --- |
| nginx | Serves the built frontend on port 80 (and each bank's port, 7001-7100), forwards `/api/` to the API | system service |
| API | FastAPI with uvicorn on `127.0.0.1:8000` | `governix-api` (systemd) |
| Worker | Processes uploaded documents (extraction, OCR, embeddings) and the scheduled jobs (SLA sweep, policy expiry) | `governix-worker` (systemd) |
| PostgreSQL 18 | Database, with the `vector`, `pg_trgm`, `unaccent` and `btree_gist` extensions | system service |
| Redis (optional) | Shared cache between API processes | system service |
| Files | Uploaded PDFs and attachments, on disk (or S3) | `/var/lib/governix/storage` |

---

## 1. Server packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl build-essential python3 python3-venv python3-dev \
    nginx tesseract-ocr tesseract-ocr-eng rsync
```

- Python: 3.11 or newer (Ubuntu 24.04 ships 3.12).
- `tesseract-ocr`: reads scanned PDFs. Add more languages as needed, e.g. `tesseract-ocr-hin`, and set `OCR_LANGUAGE`.

**Node.js 22** (only to build the frontend):

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version   # v22.x
```

**PostgreSQL 18 with pgvector** (from the PostgreSQL project's repository):

```bash
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
sudo apt install -y postgresql-18 postgresql-18-pgvector
```

**Redis** (optional, recommended when the API runs more than one process):

```bash
sudo apt install -y redis-server
```

## 2. Database

Create the database user and database. Type the password at the prompt (it never goes on the command line or
into the shell history):

```bash
sudo -u postgres createuser --pwprompt governix
sudo -u postgres createdb --owner governix governix
```

The migrations create the extensions, which needs a superuser, so create them now as `postgres`:

```bash
sudo -u postgres psql -d governix -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS unaccent; CREATE EXTENSION IF NOT EXISTS btree_gist;"
```

Keep PostgreSQL listening on localhost only (the default).

## 3. App user, code and Python packages

```bash
sudo useradd --system --create-home --home-dir /opt/governix --shell /bin/bash governix
sudo mkdir -p /var/lib/governix/storage /var/www/governix/frontend
sudo chown -R governix:governix /var/lib/governix /var/www/governix

sudo -iu governix
git clone <your-repository-url> /opt/governix/app
cd /opt/governix/app
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements/prod.txt
exit
```

`requirements/prod.txt` installs everything the app needs, including `pyotp` and `cryptography` for two-step
sign-in, and `redis`, `celery` and `boto3` for the optional production backends.

## 4. Settings (`/opt/governix/app/.env`)

The API and the worker both read `.env` from the folder they start in (`/opt/governix/app`). Create it as the
`governix` user and keep it private:

```bash
sudo -iu governix
cd /opt/governix/app
touch .env && chmod 600 .env
```

Generate the two secrets straight into the file (they are never printed):

```bash
echo "JWT_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" >> .env
echo "MFA_ENCRYPTION_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" >> .env
```

Then add the rest with an editor (`nano .env`). Replace everything in `<...>`:

```ini
# --- core -------------------------------------------------------------------
APP_ENV=production
LOG_LEVEL=INFO
LOG_JSON=true
DATABASE_URL=postgresql+psycopg://governix:<db-password>@127.0.0.1:5432/governix
DB_POOL_SIZE=12
DB_MAX_OVERFLOW=8

# --- files ------------------------------------------------------------------
STORAGE_BACKEND=local
LOCAL_STORAGE_PATH=/var/lib/governix/storage
MAX_UPLOAD_SIZE_MB=1024

# --- background work: a separate worker service (section 7) -----------------
QUEUE_BACKEND=local
WORKER_MODE=distributed
WORKER_CONCURRENCY=2

# --- cache: shared between API processes ------------------------------------
CACHE_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0

# --- AI (OpenAI) ---------------------------------------------------------------
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
OPENAI_API_KEY=<openai-api-key>

# --- sign-in, cookies, CORS ---------------------------------------------------
# Leave AUTH_COOKIE_SECURE unset: cookies are Secure on https and not on plain http (they follow the request).
# The browser reaches the API through nginx on the same address, so no other origin needs CORS.
CORS_ORIGINS=["https://<your-domain>"]
PASSWORD_RESET_URL=https://<your-domain>/reset-password

# --- email (password reset links) ---------------------------------------------
SMTP_HOST=<smtp-host>
SMTP_PORT=587
SMTP_USERNAME=<smtp-user>
SMTP_PASSWORD=<smtp-password>
SMTP_FROM=Governix <no-reply@<your-domain>>

# --- nginx portals per bank (section 8) ---------------------------------------
NGINX_CONF_DIR=/etc/nginx/governix-tenants
NGINX_FRONTEND_ROOT=/var/www/governix/frontend/dist
NGINX_API_UPSTREAM=http://127.0.0.1:8000
NGINX_TEST_COMMAND=sudo -n /usr/sbin/nginx -t
NGINX_RELOAD_COMMAND=sudo -n /usr/sbin/nginx -s reload
```

Notes:

- **`JWT_SECRET`** signs sessions. Production refuses to start without it. Changing it signs everyone out.
- **`MFA_ENCRYPTION_KEY`** encrypts the authenticator-app secrets. Back it up with the database. If it changes or is
  lost, every authenticator stops working (people sign in with a backup code or get reset by an admin). If you
  move an existing database here, copy its old `MFA_ENCRYPTION_KEY` instead of generating a new one.
- Production also refuses `STEP_UP_ENABLED=false` (important actions always need an authenticator code) and
  `RAG_RATE_LIMIT_ENABLED=false`.
- Without Redis, use `CACHE_BACKEND=memory` (a warning is logged; each API process keeps its own cache).
- Files on S3 instead of disk: `STORAGE_BACKEND=s3` with `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` (and
  `S3_ENDPOINT` / `S3_REGION` for non-AWS storage).
- The other settings and their defaults are in `app/core/config.py`.

## 5. Create the tables and the first Master Admin

Still as the `governix` user in `/opt/governix/app`:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli create-master-admin --email <admin@your-domain> --name "<Full Name>"
exit
```

The password is asked for at the prompt (never as an argument). It must meet the password policy: 12-128
characters, a capital, a small letter, a digit and a symbol, no 4 identical characters in a row, and not the
person's name or email.

**Demo data (optional, for a test or demo server only):**

```bash
.venv/bin/python -m app.cli seed
```

It creates the Master Admin `master@governix.ai`, the bank *Acme Financial Services* with branches, departments,
default categories and five users (`orgadmin@`, `branchmanager@`, `analyst@`, `compliance@`, `retail@acme.com`),
all with the password `Password@123456`, and prints them in a table. Never run it on a live server: running it
again resets those accounts' passwords to `Password@123456` and reactivates them if they were suspended.

## 6. Build the frontend

```bash
sudo -iu governix
cd /opt/governix/app/frontend
npm ci
npm run build
rsync -a --delete dist/ /var/www/governix/frontend/dist/
exit
```

The built site calls the API at `/api/...` on the same address, which nginx forwards (section 8).

## 7. Run the API and the worker (systemd)

**API**: `/etc/systemd/system/governix-api.service`

```ini
[Unit]
Description=Governix API
After=network.target postgresql.service redis-server.service
Wants=postgresql.service

[Service]
User=governix
Group=governix
WorkingDirectory=/opt/governix/app
ExecStart=/opt/governix/app/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

- `--workers 4`: about one per CPU core, up to 4-8. Each process keeps its own rate limits (AI questions,
  code attempts), so the effective limit is per process.
- `--proxy-headers`: nginx tells uvicorn whether the visitor used https, so session cookies get the `Secure` flag.

**Worker**: `/etc/systemd/system/governix-worker.service`

```ini
[Unit]
Description=Governix document worker
After=network.target postgresql.service
Wants=postgresql.service

[Service]
User=governix
Group=governix
WorkingDirectory=/opt/governix/app
ExecStart=/opt/governix/app/.venv/bin/python -m app.workers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The worker processes uploads and runs the scheduled jobs. For more throughput, raise `WORKER_CONCURRENCY` or run
a second copy of the service (jobs are never picked twice).

Start both:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now governix-api governix-worker
sudo systemctl status governix-api governix-worker
```

## 8. nginx

**Main site (port 80)**, the Master Admin portal:

```bash
cd /opt/governix/app
sudo cp deploy/nginx/governix.conf /etc/nginx/sites-available/governix.conf
sudo ln -s /etc/nginx/sites-available/governix.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
```

**Each bank's port (7001-7100)**: the app writes one file per bank when the Master Admin clicks *Deploy* in
Organizations > Nginx. Let it write there and reload nginx, and nothing else:

```bash
sudo mkdir -p /etc/nginx/governix-tenants
sudo chown governix /etc/nginx/governix-tenants
sudo cp deploy/nginx/governix-tenants.conf /etc/nginx/conf.d/governix-tenants.conf
sudo visudo -f /etc/sudoers.d/governix-nginx
```

Put this one line in the sudoers file:

```
governix ALL=(root) NOPASSWD: /usr/sbin/nginx -t, /usr/sbin/nginx -s reload
```

Check and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

More detail on the per-bank ports and their statuses: [deploy/README.md](deploy/README.md).

## 9. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 7001:7100/tcp     # the banks' ports
sudo ufw enable
```

PostgreSQL (5432), Redis (6379) and the API (8000) stay closed: they are reached on localhost only.

## 10. HTTPS (strongly recommended for a bank)

With a domain name pointing at the server:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d <your-domain>
```

Certbot adds port 443 to the main site and renews the certificate by itself. Session cookies then get the
`Secure` flag automatically. Set `CORS_ORIGINS` and `PASSWORD_RESET_URL` to the `https://` address.

The banks' ports (7001-7100) are written by the app as plain http. To serve them over https, put each bank on its
own host name and certificate instead of a port, or terminate TLS in front of nginx.

## 11. Check it works

```bash
curl -s http://127.0.0.1:8000/health          # the API itself
curl -s http://127.0.0.1/api/health           # through nginx
curl -s http://127.0.0.1/api/db-health        # the database
sudo journalctl -u governix-api -n 50 --no-pager
sudo journalctl -u governix-worker -n 50 --no-pager
```

Then open `http://<server-address>/` (or `https://<your-domain>/`), sign in as the Master Admin and:

1. Set up two-step sign-in (account menu > Two-step sign-in). Important actions need it.
2. Create an organization with its first Org Admin, set its port, and click *Deploy* in Organizations > Nginx.
3. As the Org Admin, upload a small PDF and check it reaches *Ready* (the worker is running).
4. Ask the AI Assistant a question about it (the OpenAI key works).

## 12. Updating to a new version

```bash
sudo -iu governix
cd /opt/governix/app
git pull
.venv/bin/pip install -r requirements/prod.txt
.venv/bin/alembic upgrade head
cd frontend && npm ci && npm run build && rsync -a --delete dist/ /var/www/governix/frontend/dist/
exit
sudo systemctl restart governix-api governix-worker
```

Take a database backup before `alembic upgrade head` (section 13). Browsers pick up the new frontend at once
(`index.html` is never cached; the other files have new names).

## 13. Backups

Back up three things together, at least daily, and keep a copy off the server:

| What | How |
| --- | --- |
| Database | `sudo -u postgres pg_dump -Fc governix > /backup/governix-$(date +%F).dump` |
| Uploaded files | `sudo rsync -a /var/lib/governix/storage/ /backup/storage/` |
| Settings | `/opt/governix/app/.env`: it holds `JWT_SECRET` and `MFA_ENCRYPTION_KEY` |

Restore: `sudo -u postgres pg_restore --clean -d governix /backup/governix-<date>.dump`, copy the files back to
`/var/lib/governix/storage`, restore `.env`, then restart both services.

## 14. Before going live: checklist

- [ ] `APP_ENV=production`, `JWT_SECRET` and `MFA_ENCRYPTION_KEY` set, `.env` is `chmod 600`.
- [ ] HTTPS on the main site (section 10).
- [ ] **Turn the login lockout back on.** It is switched off in the code for now: in `app/modules/auth/service.py`
      uncomment the three blocks marked `LOCKOUT DISABLED`, and remove the two `pytest.mark.skip` marks with the
      same text in the tests. Without it, password guessing at the sign-in page is not slowed down.
- [ ] `CORS_ORIGINS` lists only your own address (the default includes `*`).
- [ ] SMTP set, so password reset links are emailed.
- [ ] Every admin account has two-step sign-in set up.
- [ ] Backups run and a restore was tried once.
- [ ] Ports 5432, 6379 and 8000 are not reachable from outside.

## 15. Troubleshooting

| Symptom | Where to look |
| --- | --- |
| Site loads, every request fails (502) | `systemctl status governix-api`; `journalctl -u governix-api` |
| Uploads stay *Processing* | The worker: `systemctl status governix-worker`; `journalctl -u governix-worker` |
| API won't start: "Missing required settings" | A required setting is missing from `.env` (the message names it) |
| AI answers say the model is unavailable | `OPENAI_API_KEY` wrong or out of credit (`journalctl -u governix-api`, look for 401/429) |
| Scanned PDFs have no text | `tesseract --version`; install the language pack for `OCR_LANGUAGE` |
| Everyone's authenticator stopped working | `MFA_ENCRYPTION_KEY` changed: restore the old value |
| An admin lost their authenticator and nobody above can reset it | `sudo -iu governix` then `cd /opt/governix/app && .venv/bin/python -m app.cli reset-two-step --email <email>` |
| A bank's port shows nginx's default page | Organizations > Nginx: *Deploy config*; check `sudo nginx -t` |
| Uploads over a size fail with 413 | `client_max_body_size` in `deploy/nginx/governix.conf` and `MAX_UPLOAD_SIZE_MB` |
