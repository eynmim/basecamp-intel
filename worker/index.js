// Cloudflare Worker — Telegram webhook bridge to Gemini.
//
// Triggers when a message in the BaseCamp supergroup matches:
//   /ask <question>          → answer in the same language as the question
//   /fa <question>            → answer in Persian/Farsi regardless of input
//   /translate <text>         → translate <text> to Persian (Farsi)
//   /linkedin <topic>         → write a ready-to-publish LinkedIn post in
//                               Ali's voice (also aliased to /lp)
//   /keep                     → save the replied-to message into the 📝 Ready
//                               topic (handy for marking LinkedIn drafts you
//                               want to publish later)
//   @Chavosh2_Bot <question>  → same as /ask but via mention
//
// "Reply-to" shortcut: if the user *replies* to another message and uses
// /fa, /translate, /ask, or /keep with no inline text, the replied message
// is treated as the implicit input. So replying to a draft with /fa returns
// a Persian translation that preserves the structure, and replying with
// /keep moves the draft into the Ready topic without rewriting it.
//
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

    const text = m.text.trim();

    // /keep is handled inline (no LLM call) — copy the replied message
    // into the Ready topic. Handle before the Gemini filter so it doesn't
    // burn quota.
    if (/^\/keep(@\w+)?(\s|$)/.test(text)) {
      return await handleKeep(env, m);
    }

    // Filter: only respond to one of the supported LLM triggers.
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
    } else if (/^\/(linkedin|lp)(@\w+)?(\s|$)/.test(text)) {
      question = text.replace(/^\/(linkedin|lp)(@\w+)?\s*/, "").trim();
      mode = "linkedin";
    } else if (text.includes("@Chavosh2_Bot")) {
      question = text.replace(/@Chavosh2_Bot/g, "").trim();
      mode = "mention";
    } else {
      return new Response("ok");
    }

    // Reply-to shortcut: if the command has no inline text but the user
    // replied to a message, fall back to the replied message's text.
    // Tracked separately so the prompt builder knows it came from a reply.
    const repliedText = m.reply_to_message?.text || m.reply_to_message?.caption || "";
    const usedRepliedAsInput = !question && !!repliedText;
    if (usedRepliedAsInput) {
      question = repliedText;
    }

    if (!question) {
      await tgSend(env, m,
        "*How to use me*\n\n" +
        "• `/ask <question>` — answer in same language as your question\n" +
        "• `/fa <question>` — answer in Persian (پاسخ به فارسی)\n" +
        "• `/translate <text>` — translate to Persian (ترجمه به فارسی)\n" +
        "• `/linkedin <topic>` — write a ready-to-publish LinkedIn post (alias `/lp`)\n" +
        "• `/keep` (as reply) — save a draft into the 📝 Ready topic\n" +
        "• `@Chavosh2_Bot <question>` — same as /ask via mention\n\n" +
        "*Reply shortcuts*\n" +
        "Reply to any message and send:\n" +
        "  `/fa` or `/translate` → translate that message to Persian.\n" +
        "  `/linkedin` → turn that message into a LinkedIn post.\n" +
        "  `/keep` → copy that message into the 📝 Ready topic.\n\n" +
        "*Examples*\n" +
        "• `/ask find me embedded summer schools in Europe`\n" +
        "• `/fa شرایط بورس DAAD برای دانشجوی ایرانی چیست؟`\n" +
        "• `/translate Iranian passport holders are eligible for the EU Blue Card.`\n" +
        "• `/linkedin just upgraded ESP-IDF to v5.5.4 — BLE pairing CVE finally fixed`"
      );
      return new Response("ok");
    }

    // Wrap the question with a directive so Gemini honours the chosen mode.
    const TRANSLATE_RULES =
      "Translate the following text to Persian (Farsi). Rules:\n" +
      "- Preserve the original structure EXACTLY: bullets, numbering, line breaks, paragraph spacing.\n" +
      "- Keep technical identifiers in Latin form: chip names (ESP32, STM32, MCU), protocols (BLE, Wi-Fi), institutions (IEEE, DAAD, MIT), dates, $/€ amounts, URLs, repo names, hashtags.\n" +
      "- Use natural everyday Persian, not heavily Arabic-loaded formal register.\n" +
      "- Persian numerals (۱) inside body prose are OK, but keep dates and identifiers Latin so they remain searchable.\n" +
      "- No preamble, no commentary, no transliteration. Output ONLY the translation.";

    const LINKEDIN_RULES =
      "Write ONE ready-to-publish LinkedIn post about the topic below.\n\n" +
      "Voice: Ali Mansouri — Iranian embedded-systems engineer, MSc Embedded & " +
      "Smart Systems @ Politecnico di Torino (2025-2027). Stack: ESP32-S3, STM32, " +
      "FreeRTOS, BLE, embedded C/C++, LVGL, KiCad. GitHub: eynmim — repos: " +
      "Life_logger (ESP32-S3 dual-mic audio beamforming), STM32F411_DistanceSensor, " +
      "camera_project_repo, ROBOT. Audience: embedded engineers worldwide, hiring " +
      "managers at EU embedded firms (NXP, ST, Espressif, Nordic, IMEC, Bosch), " +
      "PoliTO MSc peers, recruiters.\n\n" +
      "Structure & style:\n" +
      "- First 2 lines = strong hook. LinkedIn truncates the rest behind 'see more'.\n" +
      "- 80-200 words total. Short paragraphs, blank line between them.\n" +
      "- First-person, conversational, technical but readable.\n" +
      "- Concrete: real version numbers, chip names, repo names, dates.\n" +
      "- Genuine voice — Ali is a learner-builder, not a thought leader.\n" +
      "- End with ONE question or CTA to invite comments.\n" +
      "- 3-4 inline hashtags at the very end (e.g., #embeddedsystems #ESP32).\n\n" +
      "Guardrails:\n" +
      "- NO buzzwords: 'revolutionary', 'game-changer', 'leverage', 'synergy'.\n" +
      "- NO openers like 'As an engineer…' or 'In today's world…'.\n" +
      "- If Ali has no hands-on experience with the topic, frame it as 'this " +
      "caught my eye' / 'TIL' — NOT 'I've been using…'.\n" +
      "- Don't pad with emoji.\n\n" +
      "Output ONLY the post text — no preamble like 'Here's a draft:', no " +
      "metadata, no commentary. Just the body the user can copy-paste straight " +
      "into LinkedIn's editor.";

    let prompt = question;
    if (mode === "translate" || (mode === "fa" && usedRepliedAsInput)) {
      // /translate <text>  OR  /fa replying to a message → translate to Persian.
      prompt = `[TRANSLATE-TO-PERSIAN]\n\n${TRANSLATE_RULES}\n\nText to translate:\n\n${question}`;
    } else if (mode === "fa") {
      // /fa <question> → answer in Persian (with replied message as context if any).
      const ctx = repliedText ? `Context (message the user is replying to):\n${repliedText}\n\n` : "";
      prompt = `[REPLY-IN-PERSIAN]\n\n${ctx}User's question:\n${question}`;
    } else if (mode === "linkedin") {
      // /linkedin <topic> (or as reply with no inline text) → polished LinkedIn post.
      const extra = repliedText && !usedRepliedAsInput
        ? `\nAdditional context (message user is replying to):\n${repliedText}\n`
        : "";
      prompt = `[WRITE-LINKEDIN-POST]\n\n${LINKEDIN_RULES}\n\nTopic:\n${question}${extra}`;
    } else if ((mode === "ask" || mode === "mention") && repliedText && !usedRepliedAsInput) {
      // /ask <question> while replying to a message → use it as context.
      prompt = `Context (message the user is replying to):\n${repliedText}\n\nUser's question:\n${question}`;
    }

    console.log(`incoming: mode=${mode}${usedRepliedAsInput ? "+reply" : (repliedText ? "+ctx" : "")} from=${m.from?.username || m.from?.id} chat=${m.chat.id} thread=${m.message_thread_id || "-"} q=${question.slice(0, 80)}`);

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
          // Bumped from 1500 → 4096: Persian/Farsi takes ~2× the tokens
          // English does for the same chars, so 1500 cut translations of
          // multi-bullet sections in half. 4096 keeps us under Telegram's
          // 4000-char message cap (worker truncates if it ever overshoots).
          generationConfig: { temperature: 0.4, maxOutputTokens: 4096 },
        }),
      }
    );
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      const cand = data?.candidates?.[0];
      const text = cand?.content?.parts?.[0]?.text;
      const finish = cand?.finishReason || "unknown";
      if (text) {
        console.log(`Gemini OK model=${model} chars=${text.length} finish=${finish}`);
        if (finish === "MAX_TOKENS") {
          console.log("WARN: response was cut at MAX_TOKENS — bump maxOutputTokens or shorten input.");
        }
        return text;
      }
      errors.push(`${model}: empty (${finish})`);
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

async function handleKeep(env, m) {
  if (!m.reply_to_message) {
    await tgSend(env, m,
      "Reply to a draft you want to save, then send `/keep`.\n\n" +
      "Example: long-press a LinkedIn draft → *Reply* → type `/keep` → send."
    );
    return new Response("ok");
  }

  const readyTopicId = parseInt(env.READY_TOPIC_ID || "", 10);
  if (!readyTopicId) {
    await tgSend(env, m,
      "📝 *Ready topic isn't configured yet.* To enable `/keep`:\n\n" +
      "1. Create a topic in BaseCamp called `📝 Ready to publish`.\n" +
      "2. Send any message inside it → long-press → *Copy Link* → " +
      "the number between the second and third slash is the topic id.\n" +
      "3. In Cloudflare Worker → Settings → Variables and Secrets → *+ Add*:\n" +
      "   • Type: Plaintext (not Secret — topic ids aren't sensitive).\n" +
      "   • Name: `READY_TOPIC_ID`\n" +
      "   • Value: that number.\n" +
      "4. Try `/keep` again."
    );
    return new Response("ok");
  }

  console.log(`keep: copying msg=${m.reply_to_message.message_id} from thread=${m.message_thread_id || "main"} to thread=${readyTopicId}`);

  const copy = await tg(env, "copyMessage", {
    chat_id: m.chat.id,
    from_chat_id: m.chat.id,
    message_id: m.reply_to_message.message_id,
    message_thread_id: readyTopicId,
  });

  if (copy.ok) {
    await tgSend(env, m,
      "✅ Saved to *📝 Ready to publish*.\n" +
      "Browse that topic whenever you're ready — copy the post text into LinkedIn."
    );
  } else {
    await tgSend(env, m, `❌ Couldn't save to Ready topic: ${copy.description || "unknown error"}`);
  }
  return new Response("ok");
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
