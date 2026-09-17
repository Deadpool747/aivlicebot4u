# BOT4U production deployment

Public URL: https://aivoicebot4u.com/ai/

The app runs in `/opt/bot4u` on the existing Lightsail instance. Nginx forwards
`/ai/` (without stripping the prefix) to `127.0.0.1:4174`. The main website
continues to use its existing upstream.

Web service: `bot4u-web.service`

- `PORT=4174`
- `BASE_PATH=/ai`
- `PUBLIC_ORIGIN=https://aivoicebot4u.com`
- Starts `node /opt/bot4u/server.cjs` as `ubuntu`.

Phone service: `bot4u-phone.service`

- Starts `/opt/bot4u/.venv/bin/python /opt/bot4u/piopiy-worker.py`.
- Uses Huzaifa's mapping and scripts in `.local`.
- Replaces `piopiy-agent@shared-92.service` for the same agent. Do not run both,
  or the local development phone worker, concurrently.

Both services start on boot and restart on failure. Nginx configuration is in
`/etc/nginx/snippets/bot4u.conf`, included by the existing site configuration.
The pre-deployment site backup is `voice-sales-agent.before-bot4u` under
`/etc/nginx/sites-available`.

Secrets and user data are not in Git. Back up `.env` and `.local` privately
before an update, and preserve them when copying application code. Existing
accounts, scripts, history and recordings were copied during initial deployment.
Subsequent local and production edits are independent.

Verification performed: HTTPS pages, scoped Secure login cookie, signup,
new-user empty scripts, script save, logout, WSS Gemini native audio, phone
worker heartbeat, and six Python call-control/recording tests. A real telephone
call after deployment still requires a user call test. Existing limitations
(including unconfigured email delivery and missing carrier CDR updates for
unanswered calls) are unchanged.

For rollback, stop `bot4u-phone`, restore the previous site configuration and
validate it with `nginx -t` before reloading Nginx. Re-enable
`piopiy-agent@shared-92` only after the BOT4U phone worker is stopped.
