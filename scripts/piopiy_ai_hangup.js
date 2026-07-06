"use strict";

const { Piopiy } = require("piopiy");

async function main() {
  const [, , conversationId, cause = "NORMAL_CLEARING", reason = "Conversation completed"] = process.argv;
  const token = process.env.PIOPIY_TOKEN || process.env.PIOPIY_API_TOKEN || process.env.AGENT_TOKEN;

  if (!token) {
    throw new Error("Missing PIOPIY_TOKEN / PIOPIY_API_TOKEN / AGENT_TOKEN");
  }
  if (!conversationId) {
    throw new Error("Missing conversation_id argument");
  }

  const client = new Piopiy(token);
  const response = await client.ai.hangup(conversationId, cause, reason);
  process.stdout.write(
    JSON.stringify({
      ok: true,
      conversation_id: conversationId,
      cause,
      reason,
      response,
    })
  );
}

main().catch((error) => {
  process.stderr.write(
    JSON.stringify({
      ok: false,
      error: error && error.stack ? error.stack : String(error),
    })
  );
  process.exit(1);
});
