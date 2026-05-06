# Deploy on AWS Lightsail

This guide deploys the FastAPI dashboard and telephony webhooks on a single Lightsail Ubuntu instance with:

- `systemd` for process management
- Nginx as reverse proxy
- Certbot TLS certificates

## 1. Create the Lightsail instance

- Platform: Linux/Unix
- Blueprint: Ubuntu 22.04 LTS (or newer)
- Plan: at least 1 GB RAM (2 GB recommended for call load)
- Open networking ports: `22`, `80`, `443`
- Attach and map a static IP

Point your DNS `A` record (for example `voice.yourdomain.com`) to the Lightsail static IP.

## 2. Server bootstrap

SSH to the instance, then run:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx
```

## 3. App setup

```bash
sudo mkdir -p /opt/new_voice_agent
sudo chown -R $USER:$USER /opt/new_voice_agent
cd /opt/new_voice_agent
git clone <your-repo-url> .
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
```

Update `.env`:

- Set `PUBLIC_BASE_URL=https://voice.yourdomain.com`
- Set your telephony provider credentials (`EXOTEL_*`, `TWILIO_*`, `AIRTEL_IQ_*`, `META_WHATSAPP_*`)
- Optional but recommended for scale:
  - `SHARED_STATE_BACKEND=redis`
  - `REDIS_URL=redis://<redis-host>:6379/0`

## 4. Install systemd service

Copy the template and adjust paths/user:

```bash
sudo cp deploy/lightsail/voice-sales-agent.service /etc/systemd/system/voice-sales-agent.service
sudo nano /etc/systemd/system/voice-sales-agent.service
```

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable voice-sales-agent
sudo systemctl start voice-sales-agent
sudo systemctl status voice-sales-agent
```

## 5. Configure Nginx reverse proxy

Copy the provided template:

```bash
sudo cp deploy/lightsail/nginx-voice-sales-agent.conf /etc/nginx/sites-available/voice-sales-agent
sudo ln -s /etc/nginx/sites-available/voice-sales-agent /etc/nginx/sites-enabled/voice-sales-agent
sudo rm -f /etc/nginx/sites-enabled/default
sudo nano /etc/nginx/sites-available/voice-sales-agent
```

Set your server name (for example `voice.yourdomain.com`), then validate and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 6. Issue TLS certificate

```bash
sudo certbot --nginx -d voice.yourdomain.com
```

Confirm HTTPS:

```bash
curl -I https://voice.yourdomain.com
```

## 7. Verify app and webhooks

- Dashboard: `https://voice.yourdomain.com`
- Twilio webhook base: `https://voice.yourdomain.com/twilio/...`
- Exotel webhook base: `https://voice.yourdomain.com/exotel/...`
- Airtel IQ webhook base: `https://voice.yourdomain.com/airtel-iq/...`
- Meta WhatsApp webhook base: `https://voice.yourdomain.com/meta-whatsapp/...`

Update the provider dashboards to use the same HTTPS base URL.

## 8. Operations quick commands

```bash
# app logs
sudo journalctl -u voice-sales-agent -f

# restart app after code or env update
sudo systemctl restart voice-sales-agent

# nginx logs
sudo tail -f /var/log/nginx/error.log /var/log/nginx/access.log
```

## 9. One-command redeploy

Use the deploy helper from your local project root:

```bash
./scripts/deploy_lightsail.sh
```

Optional: pass a different Lightsail instance name:

```bash
./scripts/deploy_lightsail.sh <instance-name>
```

This command:

- runs local Python syntax checks for key app modules
- syncs project files with `rsync --delete`
- excludes runtime/local-only data from [`deploy/lightsail/rsync-excludes.txt`](/Users/idriskhan/Documents/new_voice_agent/deploy/lightsail/rsync-excludes.txt)
- installs requirements on the server
- restarts and verifies `voice-sales-agent.service`

## Notes

- Keep `.env` private and never commit it.
- If you run multiple app workers, use shared state (`RedisCallStateStore`) as documented in [`docs/redis_scaling.md`](/Users/idriskhan/Documents/new_voice_agent/docs/redis_scaling.md).
- Lightsail has DNS/firewall propagation delay; allow a few minutes after changes.
