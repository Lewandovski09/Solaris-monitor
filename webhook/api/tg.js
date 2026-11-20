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

module.exports = async (req, res) => {
  const key = process.env.INBOX_KEY || "";
  if (req.method !== "POST") return res.status(200).send("ok");
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
  // groups: commands only; private chats: also short plain replies; button presses: always
  if (!upd.update_id) return res.status(200).send("skip");
  if (!cq && (!text || text.length > 300 || (!text.startsWith("/") && !isPrivate)))
    return res.status(200).send("skip");

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
  if (cq) return res.status(200).json({ method: "answerCallbackQuery", callback_query_id: cq.id, text: "⏳ Секунду…" });
  return res.status(200).json({ method: "sendChatAction", chat_id: msg.chat.id, action: "typing" });
};
