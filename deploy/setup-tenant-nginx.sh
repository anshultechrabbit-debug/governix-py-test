#!/usr/bin/env bash
# One-time server setup so Organizations > Nginx > Deploy config can make an organization live on its own port.
#
#   Master Admin stays where it is (e.g. http://<server>:3001); the backend stays on API_PORT (9000).
#   Each organization gets nginx on its port (7003-7100): the built frontend, with /api forwarded to the backend.
#
# Run on the server, from the project folder:
#   sudo bash deploy/setup-tenant-nginx.sh
#
# Override any default by putting it before the command:
#   sudo APP_USER=ubuntu API_PORT=9000 PUBLIC_HOST=89.116.122.215 bash deploy/setup-tenant-nginx.sh
#   FRONTEND_DIR=/var/www/my-frontend   the frontend project, when it is not the repo's frontend/
#   SKIP_BUILD=1   do not rebuild the frontend (reuse its dist/)
#   FRONTEND_READY=1 FRONTEND_ROOT=/var/www/governix-admin/dist
#                  FRONTEND_ROOT already holds the built app (written by the admin panel's own
#                  deploy/nginx-deploy.sh, with governix-config.js): serve that same folder, build and copy nothing.
#   CERT_EMAIL=ops@example.com
#                  https for custom domains: installs certbot, and a deploy then gets a Let's Encrypt certificate
#                  for the organization's domain (sent to this address for expiry notices). Without it: http only.
#
# Safe to run again: every step checks what is already there. .env is backed up before it is changed.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
API_PORT="${API_PORT:-9000}"
PUBLIC_HOST="${PUBLIC_HOST:-89.116.122.215}"
PORT_MIN="${PORT_MIN:-7003}"
PORT_MAX="${PORT_MAX:-7100}"
FRONTEND_ROOT="${FRONTEND_ROOT:-/var/www/governix/frontend/dist}"
TENANTS_DIR="${TENANTS_DIR:-/etc/nginx/governix-tenants}"
INCLUDE_FILE="/etc/nginx/conf.d/governix-tenants.conf"
SUDOERS_FILE="/etc/sudoers.d/governix-nginx"
ENV_FILE="$PROJECT_DIR/.env"
# The frontend project (has package.json and builds to dist/); defaults to the backend repo's frontend/.
FRONTEND_DIR="${FRONTEND_DIR:-$PROJECT_DIR/frontend}"
SKIP_BUILD="${SKIP_BUILD:-0}"
FRONTEND_READY="${FRONTEND_READY:-0}"
CERT_EMAIL="${CERT_EMAIL:-}"
DEFAULT_SITE="/etc/nginx/conf.d/00-governix-default.conf"
RENEW_HOOK="/etc/letsencrypt/renewal-hooks/deploy/reload-nginx"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
fail() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run with sudo: sudo bash $0"
[[ -f "$PROJECT_DIR/app/main.py" ]] || fail "$PROJECT_DIR does not look like the project folder (no app/main.py). Set PROJECT_DIR=..."
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE not found. The backend's .env must exist first."
[[ -z "$CERT_EMAIL" || "$CERT_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] \
    || fail "CERT_EMAIL must be an email address such as ops@example.com"

# --- 1. Which user runs the backend ---------------------------------------------------------------
step "Finding the user that runs the backend on port $API_PORT"
if [[ -z "${APP_USER:-}" ]]; then
    pid="$(ss -Hltnp "sport = :$API_PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)"
    [[ -n "$pid" ]] || fail "Nothing listens on port $API_PORT. Start the backend, or set APP_USER=<user> yourself."
    APP_USER="$(ps -o user= -p "$pid" | tr -d ' ')"
fi
id "$APP_USER" >/dev/null 2>&1 || fail "User '$APP_USER' does not exist."
info "Backend user: $APP_USER"

# --- 2. Packages ----------------------------------------------------------------------------------
step "Checking nginx and rsync"
missing=()
command -v nginx >/dev/null || missing+=(nginx)
command -v rsync >/dev/null || missing+=(rsync)
if [[ -n "$CERT_EMAIL" ]]; then
    command -v certbot >/dev/null || missing+=(certbot)
    dpkg -s python3-certbot-nginx >/dev/null 2>&1 || missing+=(python3-certbot-nginx)
fi
if ((${#missing[@]})); then
    info "Installing: ${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
fi
NGINX_BIN="$(command -v nginx)"
systemctl enable --now nginx >/dev/null 2>&1 || true
info "nginx: $NGINX_BIN"

# --- 3. Frontend build ----------------------------------------------------------------------------
step "Frontend for the organization ports"
if [[ "$FRONTEND_READY" == "1" ]]; then
    [[ -f "$FRONTEND_ROOT/index.html" ]] \
        || fail "$FRONTEND_ROOT/index.html is missing. Deploy the admin panel first (its deploy/nginx-deploy.sh)."
    [[ -f "$FRONTEND_ROOT/governix-config.js" ]] \
        || info "WARNING: no governix-config.js in $FRONTEND_ROOT; the app then treats port 3001 as Master Admin."
    info "Serving the admin panel's own build: $FRONTEND_ROOT (its redeploys update the banks' portals too)"
else
    [[ -f "$FRONTEND_DIR/package.json" ]] || fail "$FRONTEND_DIR has no package.json. Set FRONTEND_DIR=<your frontend folder>."
    info "Frontend project: $FRONTEND_DIR"
    if [[ "$SKIP_BUILD" != "1" ]]; then
        info "Building as $APP_USER (npm ci && npm run build)"
        sudo -u "$APP_USER" -H bash -lc "cd '$FRONTEND_DIR' && npm ci && npm run build" \
            || fail "Frontend build failed (is Node.js installed for $APP_USER?). Fix it, or build yourself and rerun with SKIP_BUILD=1."
    fi
    [[ -f "$FRONTEND_DIR/dist/index.html" ]] || fail "$FRONTEND_DIR/dist/index.html is missing. Build the frontend first."
    [[ "$(realpath -m "$FRONTEND_DIR/dist")" != "$(realpath -m "$FRONTEND_ROOT")" ]] \
        || fail "FRONTEND_ROOT must be a separate copy, not $FRONTEND_DIR/dist itself."
    mkdir -p "$FRONTEND_ROOT"
    rsync -a --delete "$FRONTEND_DIR/dist/" "$FRONTEND_ROOT/"
    # Only the copy nginx serves; never its parent, which may be a project folder holding a .env.
    chmod -R a+rX "$FRONTEND_ROOT"
    info "Copied to $FRONTEND_ROOT"
fi

# --- 4. Folder the app writes, included by nginx -----------------------------------------------------
step "nginx folder for organization sites"
mkdir -p "$TENANTS_DIR"
chown "$APP_USER" "$TENANTS_DIR"
cat > "$INCLUDE_FILE" <<EOF
# One server block per organization port, written by the app (Organizations > Nginx).
# Files ending in .conf.disabled are switched off and not included.
include $TENANTS_DIR/*.conf;
EOF
info "$TENANTS_DIR (owned by $APP_USER), included from $INCLUDE_FILE"

# Custom domains are served on port 80 (and 443) by server_name. Without a default_server on 80, nginx's first
# port-80 block takes every unknown host name and the bare IP, and conf.d (where the banks' sites are included) is
# read before sites-enabled: a bank's domain block would then answer http://<server-ip>/.
step "Default site for port 80"
port80="$("$NGINX_BIN" -T 2>/dev/null | awk -v tenants="$TENANTS_DIR/" -v ours="$DEFAULT_SITE" '
    /^# configuration file / { f = $4; sub(/:$/, "", f); next }
    index(f, tenants) == 1 || f == ours { next }
    $1 == "listen" && $2 ~ /(^|:)80;?$/ { print ($0 ~ /default_server/ ? "default " : "plain ") f }
' | sort -u)"
if grep -q '^default ' <<<"$port80"; then
    rm -f "$DEFAULT_SITE"
    info "Port 80's default_server is $(grep '^default ' <<<"$port80" | head -1 | cut -d' ' -f2-): kept"
elif [[ -n "$port80" ]]; then
    fail "Port 80 is served by $(cut -d' ' -f2- <<<"$port80" | sort -u | tr '\n' ' ')without default_server.
       Mark it as port 80's default (so a bank's domain never answers for the server's IP): in that file change
         listen 80;        ->  listen 80 default_server;
         listen [::]:80;   ->  listen [::]:80 default_server;
       then: nginx -t && systemctl reload nginx, and run this script again."
else
    cat > "$DEFAULT_SITE" <<'EOF'
# Requests for host names no site claims are dropped, so they never reach an organization's portal.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;
}
EOF
    info "$DEFAULT_SITE: unknown host names on port 80 are dropped"
fi

# --- 5. sudo rule: nginx -t and reload (and certbot) only -----------------------------------------------
step "Permission for the app to test and reload nginx"
CERTBOT_BIN="$(command -v certbot || true)"
if [[ "$APP_USER" == "root" ]]; then
    SUDO_PREFIX=""
    rm -f "$SUDOERS_FILE"
    info "Backend runs as root: no sudo rule needed"
else
    SUDO_PREFIX="sudo -n "
    allowed="$NGINX_BIN -t, $NGINX_BIN -s reload"
    [[ -n "$CERT_EMAIL" ]] && allowed+=", $CERTBOT_BIN"
    tmp="$(mktemp)"
    echo "$APP_USER ALL=(root) NOPASSWD: $allowed" > "$tmp"
    visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; fail "sudoers rule did not validate"; }
    install -m 0440 -o root -g root "$tmp" "$SUDOERS_FILE"
    rm -f "$tmp"
    info "$SUDOERS_FILE: $APP_USER may run only: $allowed"
fi

# --- 5a. https (certbot) --------------------------------------------------------------------------------
if [[ -n "$CERT_EMAIL" ]]; then
    step "https for custom domains"
    mkdir -p "$(dirname "$RENEW_HOOK")" /etc/letsencrypt/live /etc/letsencrypt/archive
    printf '#!/bin/sh\n%s -s reload\n' "$NGINX_BIN" > "$RENEW_HOOK"
    chmod 0755 "$RENEW_HOOK"
    info "Renewals reload nginx ($RENEW_HOOK); certbot renews by itself"
    if [[ "$APP_USER" != "root" ]]; then
        # The app checks that fullchain.pem and privkey.pem exist; it never reads them. Passing through the
        # folders is enough, so they stay unlistable and the key files stay root's.
        chmod 0711 /etc/letsencrypt/live /etc/letsencrypt/archive
        info "$APP_USER can see whether a certificate exists (folders 0711; keys stay root-only)"
    fi
fi

# --- 6. Firewall ----------------------------------------------------------------------------------
step "Firewall"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT_MIN:$PORT_MAX/tcp" >/dev/null
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    info "ufw: opened $PORT_MIN-$PORT_MAX, 80 and 443/tcp"
else
    info "ufw is not active: nothing to do. Open $PORT_MIN-$PORT_MAX, 80 and 443/tcp in your provider's firewall panel if it has one."
fi

# --- 7. Backend settings --------------------------------------------------------------------------
step "Backend settings in $ENV_FILE"
backup="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
cp -p "$ENV_FILE" "$backup"
info "Backup: $backup"

set_env() {  # replace KEY=... or append it; keeps the file's owner and mode
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    grep -v "^${key}=" "$ENV_FILE" > "$tmp" || true
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
    info "$key=$value"
}
set_env NGINX_CONF_DIR "$TENANTS_DIR"
set_env NGINX_FRONTEND_ROOT "$FRONTEND_ROOT"
set_env NGINX_API_UPSTREAM "http://127.0.0.1:$API_PORT"
set_env NGINX_TEST_COMMAND "${SUDO_PREFIX}$NGINX_BIN -t"
set_env NGINX_RELOAD_COMMAND "${SUDO_PREFIX}$NGINX_BIN -s reload"
set_env NGINX_PUBLIC_HOST "$PUBLIC_HOST"
set_env TENANT_PORT_MIN "$PORT_MIN"
set_env TENANT_PORT_MAX "$PORT_MAX"
set_env NGINX_DOMAIN_PORT 80
if [[ -n "$CERT_EMAIL" ]]; then
    set_env NGINX_CERTBOT_COMMAND "${SUDO_PREFIX}$CERTBOT_BIN certonly --nginx --non-interactive --agree-tos -m $CERT_EMAIL"
    set_env NGINX_CERT_DIR /etc/letsencrypt/live
fi

# --- 8. Database migration ----------------------------------------------------------------------------
step "Database migration (organizations.custom_domain)"
if [[ -x "$PROJECT_DIR/.venv/bin/alembic" ]]; then
    sudo -u "$APP_USER" -H bash -c "cd '$PROJECT_DIR' && .venv/bin/alembic upgrade head" \
        || fail "alembic upgrade head failed. See the message above."
    info "Up to date"
else
    info "No .venv/bin/alembic here. Run 'alembic upgrade head' yourself in the backend's environment."
fi

# --- 9. Check nginx -------------------------------------------------------------------------------------
step "Checking nginx"
"$NGINX_BIN" -t
systemctl reload nginx
if [[ "$APP_USER" != "root" ]]; then
    sudo -u "$APP_USER" sudo -n "$NGINX_BIN" -t >/dev/null 2>&1 \
        && info "$APP_USER can run nginx -t through sudo" \
        || fail "$APP_USER cannot run 'sudo -n $NGINX_BIN -t'. Check $SUDOERS_FILE."
fi
sudo -u "$APP_USER" test -w "$TENANTS_DIR" || fail "$APP_USER cannot write $TENANTS_DIR"
curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1 \
    && info "Backend answers on 127.0.0.1:$API_PORT" \
    || info "WARNING: no answer from http://127.0.0.1:$API_PORT/health. nginx will forward /api there."

cat <<EOF

$(printf '\033[1;32m')Done.$(printf '\033[0m')

Next:
  1. Restart the backend on port $API_PORT so it reads the new .env
     (systemctl restart <service>, pm2 restart <name>, or stop and start uvicorn).
  2. Custom domain (optional): the bank points its DNS (A record) at $PUBLIC_HOST, then
     Master Admin > Organizations > Nginx > Add Custom Domain on that organization.
  3. Master Admin > Organizations > Nginx > Deploy config on the organization.
  4. Open it on its domain (https once the certificate is issued$( [[ -n "$CERT_EMAIL" ]] || printf '; http only without CERT_EMAIL' ))
     and on http://$PUBLIC_HOST:<its port>  (the first one is $PORT_MIN).

Check from the server:
  curl -s http://127.0.0.1:$PORT_MIN/api/health
  ls $TENANTS_DIR
EOF
