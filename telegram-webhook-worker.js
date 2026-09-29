/**
 * telegram-webhook-worker.js
 *
 * Telegram -> Sara gateway for SuperSnaps.
 *
 * Telegram calls this Worker instantly when a message arrives. The Worker
 * validates Telegram's webhook secret and your allowed chat id, then forwards
 * the message to the Flask/Render Sara endpoint. The Worker sends Sara's
 * reply back through Telegram's Bot API.
 *
 * Required Worker secrets:
 *   TELEGRAM_WEBHOOK_SECRET  - Telegram setWebhook secret_token
 *   TELEGRAM_ALLOWED_CHAT_ID - your own Telegram numeric chat id
 *   TELEGRAM_BOT_TOKEN       - BotFather token used for sendMessage
 *   TELEGRAM_SARA_SECRET     - shared secret expected by the Flask app
 *   SARA_API_URL             - e.g. https://your-render-service.onrender.com/api/telegram/sara
 *
 * The old pending_expenses ingestion path is intentionally no longer used by
 * this webhook. Sara is now the conversational Telegram interface. The old
 * /api/telegram/sync route remains in the Flask app so existing data can still
 * be migrated/processed if needed.
 */

function formatTelegramHtml(text) {
  // Sara is instructed to keep Telegram replies simple. Convert the small
  // amount of Markdown she may still produce into Telegram-safe HTML.
  let value = String(text || "").replace(/\\r\\n/g, "\\n").trim();
  value = value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  value = value.replace(/^#{1,3}\\s*(.+)$/gm, "<b>$1</b>");
  value = value.replace(/\\*\\*(.+?)\\*\\*/g, "<b>$1</b>");
  value = value.replace(/__([^_]+?)__/g, "<b>$1</b>");
  value = value.replace(/\\`([^\\`]+?)\\`/g, "<code>$1</code>");
  value = value.replace(/^[ \\t]*[-*]\\s+/gm, "• ");
  value = value.replace(/^\\s*\\d+[.)]\\s+/gm, (match) => match.trimStart());

  return value || "Done.";
}

async function sendTelegramReply(env, chatId, text) {
  const formatted = formatTelegramHtml(text);

  // Telegram limits a text message to 4096 characters. Keep chunks comfortably
  // below the limit so formatted replies remain reliable.
  const chunks = [];
  for (let i = 0; i < formatted.length; i += 3800) {
    chunks.push(formatted.slice(i, i + 3800));
  }

  for (const chunk of chunks) {
    const telegramResponse = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          chat_id: chatId,
          text: chunk,
          parse_mode: "HTML",
          disable_web_page_preview: true,
        }),
      }
    );

    if (!telegramResponse.ok) {
      console.error(
        "Telegram sendMessage failed:",
        telegramResponse.status,
        await telegramResponse.text()
      );
      return;
    }
  }
}

async function processUpdate(update, env) {
  const message = update && update.message;
  const text = message && message.text;
  const chatId = message && message.chat && message.chat.id;

  if (!text || String(chatId) !== String(env.TELEGRAM_ALLOWED_CHAT_ID)) {
    return;
  }

  const saraResponse = await fetch(env.SARA_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Sara-Secret": env.TELEGRAM_SARA_SECRET,
    },
    body: JSON.stringify({
      chat_id: chatId,
      telegram_message_id: message.message_id,
      message: text,
    }),
  });

  if (!saraResponse.ok) {
    console.error("Sara request failed:", saraResponse.status, await saraResponse.text());
    return;
  }

  const result = await saraResponse.json();
  const reply = result.reply || "I couldn't generate a response right now.";
  await sendTelegramReply(env, chatId, reply);
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const secretHeader = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (secretHeader !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    const update = await request.json().catch(() => null);
    if (!update) {
      return new Response("Bad request", { status: 400 });
    }

    // Acknowledge Telegram immediately. Sara/Groq can take longer than
    // Telegram's webhook response window, so the actual work continues in
    // Cloudflare's background execution context.
    ctx.waitUntil(
      processUpdate(update, env).catch((err) => {
        console.error("Telegram Sara processing failed:", err);
      })
    );

    return new Response("ok", { status: 200 });
  },
};
