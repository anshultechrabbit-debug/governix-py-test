# Deploying Governix behind nginx

Port 80 serves the Master Admin portal. Each organization gets its own port (7001-7100, set in
Organizations > Edit). The Master Admin deploys that port from Organizations > Nginx, and the app
writes one server block per organization:

```
/etc/nginx/governix-tenants/governix-<slug>.conf            served
/etc/nginx/governix-tenants/governix-<slug>.conf.disabled   switched off
```

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

## 5. Deploy an organization

Go to Organizations > Nginx and click Deploy config. The page shows the port's address, the file, and the config
it wrote.

| Status | Meaning |
| --- | --- |
| Running | The file matches the organization's port and the settings above. |
| Out of date | The port or an `NGINX_*` setting changed since the deploy. Redeploy. |
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
