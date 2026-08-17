# Meta WhatsApp Calling Setup

This project now supports a direct Meta WhatsApp calling provider path for outbound initiation and status webhooks.

## Environment variables

Set these in `.env`:

```bash
TELEPHONY_PROVIDER=meta_whatsapp
PUBLIC_BASE_URL=https://your-public-url.example.com
META_WHATSAPP_BASE_URL=https://graph.facebook.com
META_WHATSAPP_API_VERSION=v22.0
META_WHATSAPP_ACCESS_TOKEN=your_meta_whatsapp_access_token
META_WHATSAPP_PHONE_NUMBER_ID=your_meta_whatsapp_phone_number_id
META_WHATSAPP_FROM_NUMBER=919999999999
META_WHATSAPP_INITIATE_PATH=/{api_version}/{phone_number_id}/calls
META_WHATSAPP_REQUEST_TEMPLATE_JSON={"messaging_product":"whatsapp","to":"{to_number}","action":"connect","session":{"sdp_type":"{sdp_type}","sdp":"{sdp}"},"biz_opaque_callback_data":"{pending_id}"}
META_WHATSAPP_DEFAULT_SDP_TYPE=offer
META_WHATSAPP_DEFAULT_SDP=replace_with_test_sdp_offer
META_WHATSAPP_WEBHOOK_VERIFY_TOKEN=replace_with_meta_verify_token
META_WHATSAPP_APP_SECRET=replace_with_meta_app_secret
META_WHATSAPP_POST_CALL_TEMPLATE_NAME=
META_WHATSAPP_POST_CALL_TEMPLATE_LANGUAGE_CODE=en_US
META_WHATSAPP_COST_PER_MINUTE=0
```

## Place a call

Use the existing dashboard/API flow:

- `POST /api/telephony/call`

The app will:

- create a pending call record
- call Meta initiate endpoint using the configurable request template
- persist provider `sid/status` in shared state

## Callback endpoint

Configure your provider callback URL as:

- `https://<PUBLIC_BASE_URL>/meta-whatsapp/status/<pending_id>`
- `https://<PUBLIC_BASE_URL>/meta-whatsapp/webhook` (Meta app-level webhook for `calls` field)

The app automatically generates this URL per call and sends it in the initiate payload as `{status_callback_url}`.

The callback handler:

- stores latest status payload
- updates `provider_call_sid` when present
- de-duplicates retries using webhook idempotency keys
- consumes pending state on terminal statuses/events
- verifies webhook signature when `META_WHATSAPP_APP_SECRET` is set

## Post-call WhatsApp details

To send the last-call details as a WhatsApp template message, set:

- `META_WHATSAPP_POST_CALL_TEMPLATE_NAME` to an approved WhatsApp template name
- `META_WHATSAPP_POST_CALL_TEMPLATE_LANGUAGE_CODE` to that template's language code

The app will populate the template body with:

- caller name
- caller problem
- caller location when mentioned
- confidence and session reference as extra values if the template expects them

## Notes

- This provider path currently implements call initiation and webhook state tracking.
- The app also exposes `/api/meta-whatsapp/action` to send explicit call actions (`connect`, `pre_accept`, `accept`, `reject`, `terminate`) for call-state orchestration.
- Real-time conversational media bridging is provider-dependent and must be added when Meta exposes the required streaming/control interface for your approved account.
