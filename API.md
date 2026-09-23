# BOT4U API

Base URL: https://aivoicebot4u.com/ai/api/v1

## Create and manage keys
Sign in at https://aivoicebot4u.com/ai/login, open API Keys, click Create API Key and give it a name. Copy the full token from the creation window: it is shown only once. Closing the window removes it from the page. The list retains only a masked prefix and metadata. Up to 20 active keys per account are allowed.

Create a replacement, update your integration, then revoke the old key. Revoke asks for confirmation and immediately rejects future requests; it does not cancel already submitted calls or stop running campaigns. Pause campaigns separately. Revoked records remain for audit purposes. Keys do not expire automatically.

## Authentication and security
Every endpoint below requires `Authorization: Bearer YOUR_API_KEY`. Cookies do not authenticate versioned API endpoints. Use HTTPS and keep the token in a server-side secret store. Never place it in URLs, browser JavaScript, Git, or logs. A bearer key can be replayed by anyone holding it; revoke it immediately if exposed. Keys inherit the account's existing permissions, carrier assignment and limits. They cannot create users or other keys, administer accounts, or access the browser voice WebSocket.

Requests allow 120 per account per minute and 180 per network source per minute (the reverse proxy may share this source across clients). Key management allows 30 requests per account per minute. A 429 API response includes Retry-After: 60. Last-used timestamps update at most once per minute. Keys are stored as SHA-256 hashes of 256-bit random secrets, never as plaintext.

## Routes
All bodies are JSON with Content-Type: application/json. All responses are JSON except recording audio. Parameters identifying an owner are never accepted: ownership comes from the token.

### GET /scripts?mode=inbound
Read your saved script. mode is inbound (default) or outbound.
Example: `curl 'https://aivoicebot4u.com/ai/api/v1/scripts?mode=inbound' -H "Authorization: Bearer $BOT4U_API_KEY"`
Response 200: `{"script":"Your instructions"}`. Invalid mode: 400.

### PUT /scripts?mode=outbound
Save a script for future conversations; does not alter ongoing calls. Body: script, nonblank string, maximum 30,000 characters.
Example: `curl -X PUT 'https://aivoicebot4u.com/ai/api/v1/scripts?mode=outbound' -H "Authorization: Bearer $BOT4U_API_KEY" -H 'Content-Type: application/json' -d '{"script":"Introduce yourself as BOT4U and ask how you can help."}'`
Response 200: `{"saved":true}`. Invalid body or mode: 400; oversized body: 413.

### POST /calls
Submit one outbound call using your selected carrier and saved outbound script. Body: number (E.164), name (optional, maximum 80 characters).
Example: `curl -X POST 'https://aivoicebot4u.com/ai/api/v1/calls' -H "Authorization: Bearer $BOT4U_API_KEY" -H 'Content-Type: application/json' -d '{"number":"+919876543210","name":"Aarav"}'`
Response 202: `{"historyId":"...","requestId":"...","message":"Call submitted to Airtel; ringing and answer are not yet confirmed."}`
202 means submitted, not answered. Check history. Errors: 400 invalid input/missing script; 403 no assigned number; 409 active campaign; 429 duplicate/recent call; 502 provider failure or uncertain result; 503 carrier/worker unavailable. Never automatically retry an uncertain call response: check carrier logs first. Repeated requests are not generally idempotent; existing short-term duplicate protection still applies.

### GET /call-history
Example: `curl 'https://aivoicebot4u.com/ai/api/v1/call-history' -H "Authorization: Bearer $BOT4U_API_KEY"`
Response 200: `{"calls":[{"id":"...","type":"outbound","phone":"+919876543210","name":"Aarav","duration":45,"result":"Answered","remarks":"...","recordingId":"..."}]}`
Returns only your account's calls; an empty history returns an empty array.

### PATCH /call-history
Update a call's notes/result. Body: id, remarks (maximum 2,000 characters), result (empty string to keep existing result, Answered or Not answered).
Example: `curl -X PATCH 'https://aivoicebot4u.com/ai/api/v1/call-history' -H "Authorization: Bearer $BOT4U_API_KEY" -H 'Content-Type: application/json' -d '{"id":"CALL_HISTORY_ID","remarks":"Follow up next week","result":"Answered"}'`
Response 200: `{"saved":true}`. Invalid fields: 400; missing or other account's call: 404.

### GET or HEAD /recording?id=CALL_HISTORY_ID
Use the history row id, not recordingId. Supports a single HTTP byte Range.
Example: `curl 'https://aivoicebot4u.com/ai/api/v1/recording?id=CALL_HISTORY_ID' -H "Authorization: Bearer $BOT4U_API_KEY" -o call.wav`
Response 200: audio/wav bytes; 206 partial audio; HEAD returns headers only. Unavailable or other account's recording: 404; invalid range: 416.

### GET /telephony
Example: `curl 'https://aivoicebot4u.com/ai/api/v1/telephony' -H "Authorization: Bearer $BOT4U_API_KEY"`
Response 200: `{"telephony":{"provider":"airtel_iq","outbound_provider":"airtel_iq"}}` (additional account mapping fields may be present), or `{"telephony":null}` if no assignment exists.

### PUT /telephony
Select an already assigned provider, never provision a new number. Body: provider, piopiy or airtel_iq.
Example: `curl -X PUT 'https://aivoicebot4u.com/ai/api/v1/telephony' -H "Authorization: Bearer $BOT4U_API_KEY" -H 'Content-Type: application/json' -d '{"provider":"piopiy"}'`
Response 200: same shape as GET /telephony. Invalid/unassigned provider: 400 (existing dashboard behavior); active campaign: 409.

### GET /campaign
Read your campaign and run the existing queue's progress check. This may advance a running campaign and submit its next call, just like the dashboard.
Example: `curl 'https://aivoicebot4u.com/ai/api/v1/campaign' -H "Authorization: Bearer $BOT4U_API_KEY"`
Response 200: `{"campaign":null}` or the existing campaign object, including its leads and status.

### POST /campaign
Use the dashboard's existing CSV workflow. Body: action, one of import, start, pause, confirm-ended; csv is required for import. Import previews up to 200 leads; start authorizes calls to those leads. confirm-ended acknowledges that you checked the current call has ended. Pause prevents future calls, not the current call.
Example: `curl -X POST 'https://aivoicebot4u.com/ai/api/v1/campaign' -H "Authorization: Bearer $BOT4U_API_KEY" -H 'Content-Type: application/json' -d '{"action":"import","csv":"name,phone\nAarav,+919876543210"}'`
Response 200: `{"campaign":{"...":"existing campaign fields"}}`. Invalid CSV/action/state: 400; oversized request: 413. Import alone does not start calling.

## Common errors
401: `{"error":"API key required"}` or `{"error":"Invalid or revoked API key"}`. Deleted accounts also invalidate their keys.
403: account permission or host/origin restriction. 404: unknown route/resource. 405: unsupported method. 415: JSON required. 429: rate limit exceeded. 500: server/storage failure. Errors never echo tokens. No cross-origin browser access is enabled.

Dashboard key management uses session-only GET/POST /ai/api/keys and DELETE /ai/api/keys/:id. Listing never returns tokens or hashes. Creation returns token once; responses use Cache-Control: no-store. The bearer API deliberately cannot manage keys.
