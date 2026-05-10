// Cloudflare Worker — Telegram webhook bridge to Gemini.
//
// Triggers when a message in the BaseCamp supergroup matches:
//   /ask <question>          → answer in the same language as the question
//   /fa <question>            → answer in Persian/Farsi regardless of input
//   /translate <text>         → translate <text> to Persian (Farsi)
//   @Chavosh2_Bot <question>  → same as /ask but via mention
// The Worker calls Gemini, then replies in the same topic, as a reply-to
// to the user's message.
//
// Secrets (set in Cloudflare → Workers → Settings → Variables & Secrets):
//   TELEGRAM_BOT_TOKEN
//   GEMINI_API_KEY

const TG = "https://api.telegram.org";
const GEMINI = "https://generativelanguage.googleapis.com/v1beta/models";
// Different accounts have different model availability. We try in
// preference order; the first one whose generateContent call returns
// 200 wins. If all fail, askGemini() lists what the API key CAN see
// so we know what to add.
const MODELS = [
  "gemini-2.5-flash",
  "gemini-2.5-flash-lite",
  "gemini-2.0-flash-001",
  "gemini-2.0-flash",
  "gemini-1.5-flash-002",
  "gemini-1.5-flash-latest",
  "gemini-1.5-flash-8b",
  "gemini-pro",
];

const SYSTEM_PROMPT = `You are an AI assistant inside Ali Mansouri's BaseCamp
Telegram supergroup. Ali is an embedded-systems engineer (MSc Computer
Engineering, Embedded & Smart Systems, Politecnico di Torino 2025-2027),
Iranian passport, Italian Permesso di Soggiorno (PdS) which gives him EU
mobility. His core stack: ESP32-S3, STM32, FreeRTOS, BLE, embedded C/C++,
LVGL, KiCad PCB, IoT firmware, OTA, power management. His portfolio:
github.com/eynmim — repos include Life_logger (ESP32-S3 dual-mic beamforming),
STM32F411_DistanceSensor, camera_project_repo, ROBOT.

Languages: Ali is Persian-native (مادری-زبان فارسی), fluent English,
intermediate Italian.
  - If the user writes the question in Persian/Farsi, reply in Persian.
  - If the message is preceded with "[REPLY-IN-PERSIAN]" or "[TRANSLATE-TO-PERSIAN]",
    follow that directive precisely.
  - Otherwise default to English unless the user explicitly requests another language.
  - Persian replies should use natural everyday Persian — not heavily Arabic-loaded
    formal Persian. Use Persian numerals (۱۲۳) only when listing — keep dates,
    deadlines, and technical identifiers (ESP32, BLE 5.4, etc.) in Latin form.

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

    // Filter: only respond to one of the supported triggers.
    const text = m.text.trim();
    let question = null;
    let mode = null; // "ask" | "fa" | "translate" | "mention"

    if (/^\/ask(@\w+)?(\s|$)/.test(text)) {
      question = text.replace(/^\/ask(@\w+)?\s*/, "").trim();
      mode = "ask";
    } else if (/^\/fa(@\w+)?(\s|$)/.test(text)) {
      question = text.replace(/^\/fa(@\w+)?\s*/, "").trim();
      mode = "fa";
    } else if (/^\/translate(@\w+)?(\s|$)/.test(text)) {
      question = text.replace(/^\/translate(@\w+)?\s*/, "").trim();
      mode = "translate";
    } else if (text.includes("@Chavosh2_Bot")) {
      question = text.replace(/@Chavosh2_Bot/g, "").trim();
      mode = "mention";
    } else {
      return new Response("ok");
    }

    if (!question) {
      await tgSend(env, m,
        "*How to use me*\n\n" +
        "• `/ask <question>` — answer in same language as your question\n" +
        "• `/fa <question>` — answer in Persian (پاسخ به فارسی)\n" +
        "• `/translate <text>` — translate to Persian (ترجمه به فارسی)\n" +
        "• `@Chavosh2_Bot <question>` — same as /ask via mention\n\n" +
        "*Examples*\n" +
        "• `/ask find me embedded summer schools in Europe`\n" +
        "• `/fa شرایط بورس DAAD برای دانشجوی ایرانی چیست؟`\n" +
        "• `/translate Iranian passport holders are eligible for the EU Blue Card.`"
      );
      return new Response("ok");
    }

    // Wrap the question with a directive so Gemini honours the chosen mode.
    let prompt = question;
    if (mode === "fa") {
      prompt = `[REPLY-IN-PERSIAN]\n\nThe user's question:\n${question}`;
    } else if (mode === "translate") {
      prompt = `[TRANSLATE-TO-PERSIAN]\n\nProvide ONLY the Persian translation of the text below — no preamble, no explanation, no transliteration. Keep technical identifiers (ESP32, BLE, MCU, etc.) in Latin form. Text to translate:\n\n${question}`;
    }

    console.log(`incoming: mode=${mode} from=${m.from?.username || m.from?.id} chat=${m.chat.id} thread=${m.message_thread_id || "-"} q=${question.slice(0, 80)}`);

    try {
      // Show "typing..." while Gemini works (best-effort, ignore failure).
      await tg(env, "sendChatAction", {
        chat_id: m.chat.id,
        action: "typing",
        ...(m.message_thread_id && { message_thread_id: m.message_thread_id }),
      }).catch(() => {});

      console.log("calling Gemini...");
      const answer = await askGemini(env, prompt);
      console.log(`Gemini OK, ${answer.length} chars; sending to Telegram...`);
      await tgSend(env, m, answer);
      console.log("send complete.");
    } catch (e) {
      console.log("handler error:", e.message);
      await tgSend(env, m, `❌ ${e.message || "Unknown error"}`);
    }
    return new Response("ok");
  },
};

async function askGemini(env, question) {
  if (!env.GEMINI_API_KEY) throw new Error("GEMINI_API_KEY secret is not set");

  const errors = [];
  for (const model of MODELS) {
    const r = await fetch(
      `${GEMINI}/${model}:generateContent?key=${env.GEMINI_API_KEY}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          systemInstruction: { parts: [{ text: SYSTEM_PROMPT }] },
          contents: [{ role: "user", parts: [{ text: question }] }],
          generationConfig: { temperature: 0.4, maxOutputTokens: 1500 },
        }),
      }
    );
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;
      if (text) {
        console.log(`Gemini OK with model=${model}`);
        return text;
      }
      errors.push(`${model}: empty (${data?.candidates?.[0]?.finishReason || "no text"})`);
    } else {
      const desc = data?.error?.message || `HTTP ${r.status}`;
      errors.push(`${model}: ${desc.slice(0, 100)}`);
      console.log(`Gemini ${model} failed: ${desc.slice(0, 200)}`);
    }
  }

  // All preset models failed. List what the key actually has access to so
  // the next push can target it precisely.
  let available = "(ListModels also failed)";
  try {
    const r = await fetch(`${GEMINI}?key=${env.GEMINI_API_KEY}`);
    const data = await r.json();
    if (Array.isArray(data?.models)) {
      const usable = data.models
        .filter((m) => Array.isArray(m.supportedGenerationMethods)
                       && m.supportedGenerationMethods.includes("generateContent"))
        .map((m) => (m.name || "").replace(/^models\//, ""));
      available = usable.length ? usable.slice(0, 12).join(", ") : "(no generateContent models)";
      console.log(`ListModels: ${usable.length} usable: ${usable.join(", ")}`);
    }
  } catch (e) {
    console.log("ListModels failed:", e.message);
  }

  throw new Error(
    `All Gemini models I tried failed. Available on your key: ${available}. ` +
    `Errors: ${errors.slice(0, 3).join(" | ")}`
  );
}

async function tg(env, method, payload) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("TELEGRAM_BOT_TOKEN secret is not set");
  const r = await fetch(`${TG}/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data;
  try {
    data = await r.json();
  } catch {
    data = { ok: false, description: `HTTP ${r.status} (non-JSON body)` };
  }
  if (!data.ok) {
    console.log(`tg.${method} FAILED:`, data.description, "payload:",
      JSON.stringify(payload).slice(0, 300));
  }
  return data;
}

async function tgSend(env, m, text) {
  // Telegram's hard limit is 4096; leave headroom.
  const trimmed = text.length > 4000
    ? text.slice(0, 4000) + "\n…[truncated; ask a more specific question]"
    : text;

  const base = {
    chat_id: m.chat.id,
    text: trimmed,
    reply_to_message_id: m.message_id,
    ...(m.message_thread_id && { message_thread_id: m.message_thread_id }),
  };

  // Try with Markdown first for nice formatting. If the parser rejects the
  // text (Gemini sometimes emits stray `_` or `*` that breaks Telegram's
  // Markdown V1), fall back to plain text so the user always gets the reply.
  let r = await tg(env, "sendMessage", { ...base, parse_mode: "Markdown" });
  if (r.ok) return r;

  console.log("Markdown send failed; retrying plain text.");
  r = await tg(env, "sendMessage", base);
  if (r.ok) return r;

  // Last-ditch: send a short error notice so the user knows something hit.
  console.log("Plain send also failed:", r.description);
  return tg(env, "sendMessage", {
    chat_id: m.chat.id,
    text: `❌ Telegram refused the reply: ${r.description}`,
    reply_to_message_id: m.message_id,
    ...(m.message_thread_id && { message_thread_id: m.message_thread_id }),
  });
}
