/**
 * telegram-webhook-worker.js
 *
 * Deploy this as a Cloudflare Worker (free tier). Telegram calls this the
 * instant you message your bot — no polling, no 24-hour window, since
 * this is push (webhook), not pull (getUpdates).
 *
 * All it does: verify the request really came from Telegram and from
 * YOUR chat (so nobody else who finds the bot can inject fake expenses),
 * then insert the raw message text into Supabase's `pending_expenses`
 * table. No parsing happens here — Ledger does that later, in Python,
 * using the date the message was actually SENT (not whenever it gets
 * synced) so "today"/"yesterday" resolve correctly.
 *
 * Required Worker secrets (set via `wrangler secret put <NAME>` or the
 * Cloudflare dashboard — never hardcode these):
 *   TELEGRAM_WEBHOOK_SECRET  — a random string you invent; also passed to
 *                              Telegram's setWebhook call as secret_token
 *   TELEGRAM_ALLOWED_CHAT_ID — your own Telegram numeric chat id, so
 *                              messages from anyone else are ignored
 *   SUPABASE_URL             — e.g. https://xxxx.supabase.co
 *   SUPABASE_SERVICE_KEY     — Supabase project's service_role key
 *                              (Settings -> API). Never the anon key.
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    // Telegram includes this header on every webhook call when a
    // secret_token was set via setWebhook — reject anything without it.
    const secretHeader = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (secretHeader !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    const update = await request.json().catch(() => null);
    const message = update && update.message;
    const text = message && message.text;
    const chatId = message && message.chat && message.chat.id;

    // Silently accept-and-ignore anything that isn't a plain text message
    // from your own chat, so Telegram doesn't retry delivery forever.
    if (!text || String(chatId) !== String(env.TELEGRAM_ALLOWED_CHAT_ID)) {
      return new Response("ignored", { status: 200 });
    }

    const sentAtIso = new Date(message.date * 1000).toISOString(); // Telegram gives Unix seconds

    const resp = await fetch(`${env.SUPABASE_URL}/rest/v1/pending_expenses`, {
      method: "POST",
      headers: {
        "apikey": env.SUPABASE_SERVICE_KEY,
        "Authorization": `Bearer ${env.SUPABASE_SERVICE_KEY}`,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
      },
      body: JSON.stringify({
        chat_id: chatId,
        raw_text: text,
        telegram_message_id: message.message_id,
        sent_at: sentAtIso,
      }),
    });

    if (!resp.ok) {
      // Log-visible in `wrangler tail` / the dashboard for debugging.
      console.error("Supabase insert failed:", resp.status, await resp.text());
      return new Response("storage error", { status: 502 });
    }

    return new Response("ok", { status: 200 });
  },
};
