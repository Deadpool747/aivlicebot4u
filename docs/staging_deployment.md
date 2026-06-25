# Staging Deployment

This project can now deploy staging in two safe ways:

- separate Lightsail instance
- same Lightsail instance, but isolated paths, services, and ports

Both approaches avoid reusing the live `.env`, live service names, or live remote paths.

## Recommended shape

- Separate Lightsail instance, for example `voice-agent-staging`
- Separate DNS host, for example `staging.aivoicebot4u.com`
- Separate app directory, for example `/opt/new_voice_agent_staging`
- Separate systemd units, for example:
  - `voice-sales-agent-staging.service`
  - `piopiy-agent-staging.service`

That keeps staging code, env, logs, and restarts away from production.

## Fastest Current Option

If you only have the production Lightsail instance right now, use same-host staging:

- same instance: `voice-agent-prod`
- separate app directory: `/opt/new_voice_agent_staging`
- separate Piopiy directory: `/opt/new_voice_agent_staging-piopiy`
- separate service names:
  - `voice-sales-agent-staging.service`
  - `piopiy-agent-staging.service`
- separate app port: `8001`
- separate HTTPS entrypoint through Nginx: `https://aivoicebot4u.com:4443`

This avoids DNS and certificate work because it reuses the existing domain certificate on a different HTTPS port.

## 1. Create a staging env file

Start from:

- [`.env.example`](/Users/idriskhan/Documents/new_voice_agent/.env.example)
- [`.env.staging.example`](/Users/idriskhan/Documents/new_voice_agent/.env.staging.example)

Suggested flow:

```bash
cp .env.example .env.staging
```

Then update at least:

- `PUBLIC_BASE_URL`
- `TRUSTED_HOSTS`
- `PIOPIY_WS_PUBLIC_BASE_URL`
- provider credentials if you want staging to use different tokens

## 2. Prepare the staging instance

Follow the normal Lightsail bootstrap guide in [`docs/lightsail_deployment.md`](/Users/idriskhan/Documents/new_voice_agent/docs/lightsail_deployment.md), but use the staging instance and staging DNS name.

## 3. Deploy to staging

Run the deploy helper with staging-specific overrides:

```bash
ENV_FILE=.env.staging \
REMOTE_APP_DIR=/opt/new_voice_agent_staging \
REMOTE_PIOPIY_DIR=/opt/new_voice_agent_staging-piopiy \
VOICE_SERVICE_NAME=voice-sales-agent-staging.service \
PIOPIY_SERVICE_NAME=piopiy-agent-staging.service \
./scripts/deploy_lightsail.sh voice-agent-staging
```

What this now does safely:

- reads values from `.env.staging` instead of the live `.env`
- renders systemd unit files with the staging paths
- installs them under staging-specific service names
- restarts only the staging services

For the same-host staging shortcut already added in this repo, run:

```bash
./scripts/deploy_lightsail_staging.sh
```

That defaults to:

- instance: `voice-agent-prod`
- app port: `8001`
- staging service names
- staging remote directories

## 4. Configure Nginx on staging

Use the existing template:

- [`deploy/lightsail/nginx-voice-sales-agent.conf`](/Users/idriskhan/Documents/new_voice_agent/deploy/lightsail/nginx-voice-sales-agent.conf)

For same-host staging on HTTPS port `4443`, use:

- [`deploy/lightsail/nginx-voice-sales-agent-staging-4443.conf`](/Users/idriskhan/Documents/new_voice_agent/deploy/lightsail/nginx-voice-sales-agent-staging-4443.conf)

Change:

- `server_name`
- certificate paths

to your staging host, then reload Nginx on the staging instance.

## 5. Verify isolation

On staging, check:

```bash
sudo systemctl status voice-sales-agent-staging.service
sudo systemctl status piopiy-agent-staging.service
```

On production, confirm the live services were not restarted:

```bash
sudo systemctl status voice-sales-agent.service
sudo systemctl status piopiy-agent.service
```

## Notes

- If staging should not receive real inbound traffic, do not map the production Piopiy number to the staging callback URL.
- If you want to test telephony end to end, use a separate Piopiy app or temporary callback mapping pointed at the staging host.
