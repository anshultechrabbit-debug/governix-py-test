# Deploying Governix behind nginx

Port 80 (or 7001 on an IP-based server) serves the Master Admin portal. Each organization gets its own port
(TENANT_PORT_MIN-TENANT_PORT_MAX, 7003-7100 by default): the next free one when it is created, changeable in
Organizations > Edit. The Master Admin deploys that port from Organizations > Nginx, and the app
writes one server block per organization:

```
/etc/nginx/governix-tenants/governix-<slug>.conf            served
/etc/nginx/governix-tenants/governix-<slug>.conf.disabled   switched off
```

With `NGINX_BASE_DOMAIN` the same file also serves the bank on `<slug>.<base domain>`, with HTTPS when certbot is
set up (section 4a); the files can also live in sites-available with a link in sites-enabled (section 4b).

Every port serves the same built frontend and forwards `/api/` to uvicorn. A deploy runs `nginx -t`
before reloading. If either step fails, the previous file is put back and nginx's message is shown
on the page, so a deploy never leaves nginx broken.

In the steps below, `<app-user>` is the Linux user that runs uvicorn.

## 1. Build the frontend

```
cd frontend
npm ci
npm run build
sudo mkdir -p /var/www/governix/frontend
sudo rsync -a --delete dist/ /var/www/governix/frontend/dist/
```

## 2. The main site (port 80)

```
sudo cp deploy/nginx/governix.conf /etc/nginx/sites-available/governix.conf
sudo ln -s /etc/nginx/sites-available/governix.conf /etc/nginx/sites-enabled/
```

If another site already listens on port 80 (for example the default site), remove it from
`sites-enabled`.

## 3. The organization ports

Create a folder that the app can write and nginx includes:

```
sudo mkdir -p /etc/nginx/governix-tenants
sudo chown <app-user> /etc/nginx/governix-tenants
sudo cp deploy/nginx/governix-tenants.conf /etc/nginx/conf.d/governix-tenants.conf
```

Let the app check and reload nginx, and nothing else. Create `/etc/sudoers.d/governix-nginx` with
`sudo visudo -f /etc/sudoers.d/governix-nginx`:

```
<app-user> ALL=(root) NOPASSWD: /usr/sbin/nginx -t, /usr/sbin/nginx -s reload
```

Open the ports in the firewall:

```
sudo ufw allow 7001:7100/tcp
```

## 4. Backend settings (`.env`)

```
NGINX_CONF_DIR=/etc/nginx/governix-tenants
NGINX_FRONTEND_ROOT=/var/www/governix/frontend/dist
NGINX_API_UPSTREAM=http://127.0.0.1:8000
NGINX_TEST_COMMAND=sudo -n /usr/sbin/nginx -t
NGINX_RELOAD_COMMAND=sudo -n /usr/sbin/nginx -s reload
```

Optional: `NGINX_SERVER_NAME` (default `_`, any host name, because organizations are told apart by port).

Then run `sudo nginx -t && sudo systemctl reload nginx`, and restart uvicorn so it reads the new settings.

### 4a. Each bank on its own domain (optional)

Like the Node backend's `DEFAULT_BASE_DOMAIN`: with this set, one deploy serves a bank on its port **and** on
`<slug>.<base domain>` (for example `hdfc.governix.example.com`), in the same file.

```
NGINX_BASE_DOMAIN=governix.example.com
NGINX_DOMAIN_PORT=80
```

DNS: one wildcard record, `*.governix.example.com` → this server's IP. Keep the main site's `listen 80` marked
`default_server`, so an unknown host name never lands on a bank. The sign-in page finds the bank from the address
it was opened on (`GET /api/tenant-info?host=&port=`, public: name, colors, font, logo only).

**HTTPS for the domains**: install certbot (`sudo apt install -y certbot python3-certbot-nginx`) and set

```
NGINX_CERTBOT_COMMAND=certbot certonly --nginx --non-interactive --agree-tos -m ops@your-bank.com
NGINX_CERT_DIR=/etc/letsencrypt/live
```

(prefix `sudo -n` and allow `/usr/bin/certbot` in the sudoers rule when uvicorn is not root). On a deploy, a domain
without a certificate gets one (`--cert-name governix-<slug>`), and the file is rewritten with 443 and an http →
https redirect. If certbot fails (DNS not pointing here yet), the site stays live on http and the page shows
certbot's message; deploy again once DNS is right. Certbot renews by itself; add a deploy hook that reloads nginx
(`/etc/letsencrypt/renewal-hooks/deploy/reload-nginx`: `#!/bin/sh` and `nginx -s reload`).

### 4b. sites-available / sites-enabled instead of an included folder

To keep the files next to the server's other sites, as the Node backend does:

```
NGINX_CONF_DIR=/etc/nginx/sites-available
NGINX_ENABLED_DIR=/etc/nginx/sites-enabled
```

A deploy writes `sites-available/governix-<slug>.conf` and links it into `sites-enabled`; Disable removes the link.
No `conf.d` include is needed. The files are always `governix-<slug>.conf`, so other sites' files are never touched.

## 5. Deploy an organization

Go to Organizations > Nginx and click Deploy config. The page shows the port's address, the file, and the config
it wrote.

| Status | Meaning |
| --- | --- |
| Running | The file matches the organization's port, domain and certificate, and the settings above. |
| Out of date | The port, a certificate or an `NGINX_*` setting changed since the deploy, or the sites-enabled link is missing. Redeploy. |
| Disabled | Switched off. Enable writes it again. |
| Not deployed | No file yet. |
| Manual | `NGINX_CONF_DIR` is not set. Copy the config from the page into nginx by hand. |

Deploys and disables are recorded in the audit log (`organization.nginx_deployed`,
`organization.nginx_disabled`).

## Trying it on a Mac

Homebrew's nginx already includes `/opt/homebrew/etc/nginx/servers/*`, and runs as you, so no
sudo is needed:

```
NGINX_CONF_DIR=/opt/homebrew/etc/nginx/servers
NGINX_FRONTEND_ROOT=/Users/<you>/governix-py-test/frontend/dist
```

Build the frontend first (`npm run build`), and keep uvicorn on port 8000.
