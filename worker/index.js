// Cloudflare Worker — Telegram webhook bridge to Gemini.
//
// Triggers when a message in the BaseCamp supergroup either:
//   (a) starts with "/ask <question>", or
//   (b) mentions @Chavosh2_Bot.
// The Worker calls Gemini, then replies in the same topic, as a reply-to
// to the user's message.
//
// Secrets (set in Cloudflare → Workers → Settings → Variables & Secrets):
//   TELEGRAM_BOT_TOKEN
//   GEMINI_API_KEY

const TG = "https://api.telegram.org";
const GEMINI = "https://generativelanguage.googleapis.com/v1beta/models";
const MODEL = "gemini-2.0-flash";

const SYSTEM_PROMPT = `You are an AI assistant inside Ali Mansouri's BaseCamp
Telegram supergroup. Ali is an embedded-systems engineer (MSc Computer
Engineering, Embedded & Smart Systems, Politecnico di Torino 2025-2027),
Iranian passport, Italian Permesso di Soggiorno (PdS) which gives him EU
mobility. His core stack: ESP32-S3, STM32, FreeRTOS, BLE, embedded C/C++,
LVGL, KiCad PCB, IoT firmware, OTA, power management. His portfolio:
github.com/eynmim — repos include Life_logger (ESP32-S3 dual-mic beamforming),
STM32F411_DistanceSensor, camera_project_repo, ROBOT.

Your job: answer questions about embedded jobs, scholarships, summer schools,
courses, EU mobility, and engineering deep-dives. Be CONCISE and TECHNICAL.
No marketing fluff.

When asked about an opportunity (scholarship/job/internship/summer school):
  - Verify Iranian-passport eligibility (he can't apply to ITAR-restricted ones).
  - Italian PdS gives him Schengen mobility — leverage for EU positions.
  - Match the position against his stack: ESP32, STM32, FreeRTOS, BLE, etc.
  - Always include a direct application/info link.
  - Surface deadline, stipend/award, eligibility, and what to prepare.
  - If you don't know a fact (e.g. exact deadline, exact stipend), say so —
    DO NOT invent. Suggest where he can verify (program's official page).

When the user shares a position name and asks for "more details":
  - Expand into: deadline, stipend, eligibility, application materials,
    estimated effort, why-it-fits-his-profile.
  - Mention if you're uncertain about any field.

Style: Markdown for emphasis, but Telegram supports limited Markdown — keep
formatting light. Bold for headlines, bullets for lists. No tables.`;

export default {
  async fetch(req, env) {
    if (req.method !== "POST") return new Response("ok");

    let update;
    try {
      update = await req.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    const m = update.message || update.edited_message;
    if (!m?.text || !m?.chat?.id) return new Response("ok");

    // Filter: only respond when the user opted in via /ask or @-mention.
    const text = m.text.trim();
    let question = null;
    if (text.startsWith("/ask")) {
      question = text.replace(/^\/ask(@\w+)?\s*/, "").trim();
    } else if (text.includes("@Chavosh2_Bot")) {
      question = text.replace(/@Chavosh2_Bot/g, "").trim();
    } else {
      return new Response("ok");
    }

    if (!question) {
      await tgSend(env, m,
        "Send `/ask <your question>` or mention me with the question.\n\n" +
        "Examples:\n" +
        "• `/ask find me embedded summer schools in Europe with deadlines next month`\n" +
        "• `@Chavosh2_Bot give me details about KAIST embedded MSc — eligibility, stipend, deadline`"
      );
      return new Response("ok");
    }

    try {
      // Show "typing..." while Gemini works (best-effort, ignore failure).
      await tg(env, "sendChatAction", {
        chat_id: m.chat.id,
        action: "typing",
        ...(m.message_thread_id && { message_thread_id: m.message_thread_id }),
      }).catch(() => {});

      const answer = await askGemini(env, question);
      await tgSend(env, m, answer);
    } catch (e) {
      await tgSend(env, m, `❌ ${e.message || "Unknown error"}`);
    }
    return new Response("ok");
  },
};

async function askGemini(env, question) {
  if (!env.GEMINI_API_KEY) throw new Error("GEMINI_API_KEY secret is not set");

  const r = await fetch(
    `${GEMINI}/${MODEL}:generateContent?key=${env.GEMINI_API_KEY}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        systemInstruction: { parts: [{ text: SYSTEM_PROMPT }] },
        contents: [{ role: "user", parts: [{ text: question }] }],
        generationConfig: {
          temperature: 0.4,
          maxOutputTokens: 1500,
        },
      }),
    }
  );
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const msg = data?.error?.message || `Gemini HTTP ${r.status}`;
    throw new Error(msg);
  }
  const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!text) {
    const reason = data?.candidates?.[0]?.finishReason || "no text";
    throw new Error(`Gemini returned no text (${reason})`);
  }
  return text;
}

async function tg(env, method, payload) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("TELEGRAM_BOT_TOKEN secret is not set");
  return fetch(`${TG}/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function tgSend(env, m, text) {
  // Telegram's hard limit is 4096; leave headroom.
  const trimmed = text.length > 4000
    ? text.slice(0, 4000) + "\n…[truncated; ask a more specific question]"
    : text;
  return tg(env, "sendMessage", {
    chat_id: m.chat.id,
    text: trimmed,
    parse_mode: "Markdown",
    reply_to_message_id: m.message_id,
    ...(m.message_thread_id && { message_thread_id: m.message_thread_id }),
  });
}
