// Telegram webhook receiver for the Solaris monitor.
// It stores the incoming bot command encrypted in the repo (inbox folder) and wakes the GitHub workflow.
const crypto = require("crypto");

const REPO = process.env.GITHUB_REPO || "Lewandovski09/Solaris-monitor";

function gh(path, method, body) {
  return fetch("https://api.github.com/repos/" + REPO + path, {
    method,
    headers: {
      Authorization: "Bearer " + process.env.GITHUB_TOKEN,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "solaris-webhook",
    },
    body: JSON.stringify(body),
  });
}

// Fast path: ready-made answers for simple buttons (status, schedule, help, object menu). The GitHub run writes them
// every few minutes to the "snapshot" branch, encrypted with INBOX_KEY. Answering from it takes about a second
// instead of waiting 30-60 s for a GitHub run. Anything else (or a stale snapshot) goes the usual way.
async function snapshot(key) {
  const r = await fetch("https://api.github.com/repos/" + REPO + "/contents/snapshot.enc?ref=snapshot", {
    headers: { Authorization: "Bearer " + process.env.GITHUB_TOKEN, Accept: "application/vnd.github.raw", "User-Agent": "solaris-webhook" },
  });
  if (!r.ok) return null;
  const [iv, body] = (await r.text()).split("\n");
  const d = crypto.createDecipheriv("aes-256-cbc", crypto.createHash("sha256").update(key).digest(), Buffer.from(iv.trim(), "hex"));
  const snap = JSON.parse(Buffer.concat([d.update(Buffer.from(body.trim(), "base64")), d.final()]).toString("utf8"));
  snap.age = Date.now() / 1000 - snap.at;
  return snap.age < 600 ? snap : null; // older than 10 minutes - not trusted
}

// «На скільки вистачить заряду»: клиент ждёт данные на этот момент - снимок не старше 7 минут, иначе бот спросит Deye сам
const LEFT_KEYS = new Set(["⏳ На скільки вистачить заряду", "/zaryad"]);
function needsFresh(cq, look) { return cq ? /^o:[^:]+:left$/.test(look) : LEFT_KEYS.has(look); }

async function tgCall(method, body) {
  return fetch("https://api.telegram.org/bot" + process.env.TELEGRAM_TOKEN + "/" + method, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}

async function fast(key, upd) {
  const cq = upd.callback_query;
  const msg = upd.message || {};
  const chat = String(cq ? ((cq.message || {}).chat || {}).id : (msg.chat || {}).id);
  const staffChat = String(process.env.TELEGRAM_CHAT_ID || "");
  const isStaff = staffChat && chat === staffChat;
  if (!cq && (msg.chat || {}).type !== "private" && !isStaff) return null;
  let look = cq ? String(cq.data || "") : String(msg.text || "").trim().split("@")[0];
  if (!cq && isStaff) {
    // группа: «/link@Solaris_deye_bot sh_home» -> «/link sh_home», регистр не важен
    const parts = String(msg.text || "").trim().split(/\s+/);
    look = [parts[0].split("@")[0]].concat(parts.slice(1)).join(" ").toLowerCase();
  }
  let snap;
  try { snap = await snapshot(key); } catch (e) { return null; }
  const c = snap && snap.chats && snap.chats[chat];
  if (!c) return null;
  if (needsFresh(cq, look) && snap.age > 420) return null;
  const sv = saveFast(snap, c, chat, cq, msg);
  if (sv) return sv;
  const ans = ((cq ? c.cb : c.t) || {})[look];
  if (!ans) return null;
  if (ans.m) return { chat, ch: !!ans.ch, h: ans.h, m: ans.m, clear: snap.clear || [] };
  if (!Array.isArray(ans)) return { chat, ch: false, m: [[ans, c.kb]] }; // старый формат снимка
  return { chat, ch: false, m: [[ans[0], ans[1] || c.kb]] };
}

// ---- экономия: шаблон «в общем виде» из снимка + цены из кнопки или из ответа клиента
const VERSION = "2026-11-27b";
const PRICE_URL = "https://solaris.org.ua/#price:";

function grn(v) {
  const r = Math.round(Math.abs(v));
  return (v < 0 && r ? "-" : "") + String(r).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}
function num(x) { return String(Number(Number(x).toPrecision(6))); }

function renderSave(snap, sid, per, T, G) {
  const s = (snap.save || {})[sid];
  const tpl = s && s.tpl && s.tpl[per];
  if (!tpl || !(T > 0) || !(G > 0)) return null;
  const text = tpl.replace(/⟦T⟧/g, num(T)).replace(/⟦G⟧/g, num(G)).replace(/⟦([-\d.]+);([-\d.]+);([-\d.]+)(;s)?⟧/g,
    (m, c, a, b, sg) => { const v = +c + a * T + b * G; return sg ? (v >= 0 ? "+" : "−") + grn(Math.abs(v)) : grn(v); });
  if (text.includes("⟦")) return null;
  return [text, kbWith(s.kb, T, G)];
}
function kbWith(kb, T, G) {
  return { inline_keyboard: JSON.parse(JSON.stringify(kb).replace(/\{T\}/g, num(T)).replace(/\{G\}/g, num(G))) };
}

function saveFast(snap, c, chat, cq, msg) {
  try {
    if (cq) {
      const d = String(cq.data || "").split(":");
      if (d[0] !== "sv" || !(c.sids || []).includes(d[1])) return null;
      const s = (snap.save || {})[d[1]] || {};
      const T = d.length >= 5 ? Number(d[3]) : s.T, G = d.length >= 5 ? Number(d[4]) : s.G;
      const r = renderSave(snap, d[1], d[2], T, G);
      return r ? { chat, ch: false, m: [r], clear: snap.clear || [] } : null;
    }
    // ответ на вопрос о цене: записываем сразу (бот применит молча) и показываем периоды
    const rep = msg.reply_to_message || {};
    const e = (rep.entities || []).find(x => String(x.url || "").startsWith(PRICE_URL));
    if (!e) return null;
    const [kind, sid] = String(e.url).slice(PRICE_URL.length).split(":");
    if (!(c.sids || []).includes(sid)) return null;
    const m = String(msg.text || "").match(/\d+(?:[.,]\d+)?/);
    const val = m ? Number(m[0].replace(",", ".")) : NaN;
    const [lo, hi] = kind === "tariff" ? [0.5, 50] : [5, 200];
    if (!(val >= lo && val <= hi)) return null;
    const s = (snap.save || {})[sid];
    if (!s) return null;
    const T = kind === "tariff" ? val : s.T, G = kind === "gen" ? val : s.G;
    if (!(T > 0) || !(G > 0)) return null;
    const what = kind === "tariff" ? "світло з мережі" : "кВт·год від генератора";
    const text = `✅ Записано: ${what} — ${num(val)} грн. ${snap.saved_txt}`;
    const h = crypto.createHash("sha1").update(text, "utf8").digest("hex").slice(0, 12);
    return { chat, ch: true, h, m: [[text, kbWith(s.kb, T, G)]], clear: snap.clear || [] };
  } catch (err) { return null; }
}

function msgBody(chat, text, markup) {
  const b = { chat_id: chat, text: String(text).slice(0, 4000), parse_mode: "HTML", disable_web_page_preview: true };
  if (markup) b.reply_markup = markup;
  return b;
}

module.exports = async (req, res) => {
  const key = process.env.INBOX_KEY || "";
  if (req.method !== "POST") {
    // проверка быстрых ответов: ?probe=<первые 7 знаков последнего коммита main> - без данных клиентов, только возраст снимка
    const q = (req.query && req.query.probe) || "";
    if (q && process.env.GITHUB_TOKEN) {
      const r = await gh("/commits/main", "GET");
      const sha = r.ok ? String((await r.json()).sha).slice(0, 7) : "";
      if (q === sha) {
        const t0 = Date.now();
        let info, snap;
        try {
          const rr = await fetch("https://api.github.com/repos/" + REPO + "/contents/snapshot.enc?ref=snapshot", {
            headers: { Authorization: "Bearer " + process.env.GITHUB_TOKEN, Accept: "application/vnd.github.raw", "User-Agent": "solaris-webhook" } });
          const [iv, body] = (await rr.text()).split("\n");
          const d = crypto.createDecipheriv("aes-256-cbc", crypto.createHash("sha256").update(key).digest(), Buffer.from(iv.trim(), "hex"));
          snap = JSON.parse(Buffer.concat([d.update(Buffer.from(body.trim(), "base64")), d.final()]).toString("utf8"));
          info = "snapshot ok, age " + Math.round(Date.now() / 1000 - snap.at) + " s, chats " + Object.keys(snap.chats || {}).length;
        } catch (e) { info = "snapshot error: " + String(e.message || e).slice(0, 80); }
        info += "; v " + VERSION + "; save " + Object.keys(snap && snap.save || {}).length;
        return res.status(200).send(info + "; fetch+decrypt " + (Date.now() - t0) + " ms; telegram token " + (process.env.TELEGRAM_TOKEN ? "set" : "missing"));
      }
    }
    return res.status(200).send("ok");
  }
  if (!key || !process.env.GITHUB_TOKEN) return res.status(500).send("not configured");
  const want = crypto.createHash("sha256").update("tg:" + key).digest("hex");
  const got = String(req.headers["x-telegram-bot-api-secret-token"] || "");
  if (got.length !== want.length || !crypto.timingSafeEqual(Buffer.from(got), Buffer.from(want)))
    return res.status(403).send("forbidden");

  const upd = req.body || {};
  const cq = upd.callback_query;
  const msg = upd.message || {};
  const text = (msg.text || "").trim();
  const isPrivate = (msg.chat || {}).type === "private";
  const staffChat = String(process.env.TELEGRAM_CHAT_ID || "");
  const rep = msg.reply_to_message || {};
  // ответ сотрудника (свайпом) на сообщение бота в группе - может быть ответом на вопрос клиента
  const staffReply = !!staffChat && String((msg.chat || {}).id) === staffChat && !!(rep.from && rep.from.is_bot);
  const media = ["photo", "document", "video", "voice", "audio", "video_note", "sticker", "animation"].some(k => msg[k]);
  // кнопки - всегда; команды - везде; личка клиента - любой текст или вложение (вопросы); группа - ответы на сообщения бота
  if (!upd.update_id) return res.status(200).send("skip");
  const ok = cq || (text.startsWith("/") && text.length <= 300) || ((isPrivate || staffReply) && (text || media) && text.length <= 4000);
  if (!ok) return res.status(200).send("skip");

  const quick = await fast(key, upd);
  if (quick && process.env.TELEGRAM_TOKEN) {
    // мгновенный ответ из снимка
    if (cq && cq.message && quick.clear && quick.clear.includes(String(cq.data || "").split(":")[0]) && cq.data !== "cq:?")
      tgCall("editMessageReplyMarkup", { chat_id: quick.chat, message_id: cq.message.message_id, reply_markup: { inline_keyboard: [] } });
    for (const [t, mk] of quick.m) await tgCall("sendMessage", msgBody(quick.chat, t, mk));
    if (quick.ch) {
      // нажатие что-то меняет: передаём боту, он применит молча (и пришлёт правильный ответ, если этот устарел)
      upd._answered = true; upd._chat = quick.chat; upd._h = quick.h;
    } else {
      return cq ? res.status(200).json({ method: "answerCallbackQuery", callback_query_id: cq.id }) : res.status(200).send("ok");
    }
  } else if (quick && !cq && quick.m.length === 1 && !quick.ch) {
    return res.status(200).json(Object.assign({ method: "sendMessage" }, msgBody(quick.chat, quick.m[0][0], quick.m[0][1])));
  }

  const iv = crypto.randomBytes(16);
  const c = crypto.createCipheriv("aes-256-cbc", crypto.createHash("sha256").update(key).digest(), iv);
  const enc = Buffer.concat([c.update(JSON.stringify(upd), "utf8"), c.final()]).toString("base64");
  const file = iv.toString("hex") + "\n" + enc + "\n";

  const put = await gh("/contents/inbox/" + upd.update_id + ".txt", "PUT", {
    message: "inbox",
    content: Buffer.from(file).toString("base64"),
  });
  if (!put.ok && put.status !== 422) return res.status(502).send("store failed " + put.status);
  const run = await gh("/actions/workflows/monitor.yml/dispatches", "POST", { ref: "main", inputs: { mode: "cmd" } });
  if (!run.ok) return res.status(502).send("dispatch failed " + run.status);
  // Telegram lets a webhook answer with one API call: stop the button spinner / show "typing"
  if (cq) return res.status(200).json(Object.assign({ method: "answerCallbackQuery", callback_query_id: cq.id }, upd._answered ? {} : { text: "⏳ Секунду…" }));
  if (upd._answered) return res.status(200).send("ok");
  return res.status(200).json({ method: "sendChatAction", chat_id: msg.chat.id, action: "typing" });
};
