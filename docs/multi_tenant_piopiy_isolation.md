# Multi-Tenant Piopiy Isolation

Piopiy inbound calls are tenant-routed by the called DID only.

Each client-owned Piopiy number must be configured in that client's project
runtime under `piopiy_dids`. The runtime `piopiy_caller_id` is used for
outbound caller ID. For an inbound project, these are usually the same number.

Example:

```json
{
  "project_id": "client_inbound_project",
  "runtime": {
    "outbound_call_provider": "piopiy",
    "piopiy_agent_id": "client-agent-id",
    "piopiy_caller_id": "91XXXXXXXXXX",
    "piopiy_dids": ["91XXXXXXXXXX"],
    "piopiy_app_id": "optional-piopiy-app-id"
  }
}
```

Rules:

- A DID must appear in only one client's `piopiy_dids`.
- Unknown inbound DIDs are rejected instead of falling back to another client.
- Duplicate DID ownership is treated as a server configuration error.
- Outbound Piopiy calls validate that the caller ID belongs to the same client.
- Global `.env` Piopiy client/project values are fallback process defaults only;
  they are not used to select an inbound tenant.

Current explicit DID owners:

- `917943446880` -> `user_janjal_voicebot_12c92bbc` / `janjal_ward22_inbound_918065254654`
- `917943444692` -> `user_oswelltechnologies_co_f2238ea2` / `sales_agent_3`

To add a future client:

1. Create the client folder/config.
2. Create or update its project runtime.
3. Add the assigned Piopiy DID to that project's `piopiy_dids`.
4. Set the matching outbound `piopiy_caller_id` if outbound calls are enabled.
5. Ensure no other client/project owns the same DID.

If the Piopiy dashboard maps that number to a distinct Piopiy AI agent ID,
run a dedicated worker instance for that agent instead of changing another
client's worker. A single worker can serve multiple clients on the same Piopiy
agent ID, as long as every client has a unique DID in `piopiy_dids`.

```bash
sudo mkdir -p /opt/new_voice_agent/runtime/piopiy-workers
sudo tee /opt/new_voice_agent/runtime/piopiy-workers/shared-agent-slug.env >/dev/null <<'ENV'
AGENT_ID=shared-piopiy-agent-id
PIOPIY_AGENT_ID=shared-piopiy-agent-id
PIOPIY_CLIENT_ID=aivoicebot4u_guest_demo
PIOPIY_PROJECT_ID=real_estate_english_demo
PIOPIY_CALLER_ID=
PIOPIY_TRACE_FILE=/opt/new_voice_agent/runtime/piopiy_agent_shared-agent-slug_trace.jsonl
ENV

sudo systemctl enable --now piopiy-agent@shared-agent-slug.service
```

Do not reuse another client's worker for a different Piopiy agent ID. Do not run
two worker services for the same Piopiy agent ID.

Current production worker layout:

- `piopiy-agent.service`: Janjal-only Piopiy agent `97b0c274-159d-4cdc-9901-1e1a1fba820c`.
- `piopiy-agent@shared-92.service`: shared Piopiy agent `92c3097f-e6c0-48cd-894b-3916dc30ed7e`; tenant is selected by DID.
