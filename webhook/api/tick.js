// Timer endpoint for the Solaris monitor: an external scheduler (cron-job.org) opens this URL,
// and it starts one station check on GitHub. No secrets in the URL: repeated calls are harmless,
// because a check is skipped when one ran less than 100 seconds ago or is still running.
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
    body: body ? JSON.stringify(body) : undefined,
  });
}

// Alert to the staff Telegram group straight from Vercel, without GitHub: for the case when the bot itself is down.
// Needs TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in Vercel env. At most about once per 30 minutes (the timer calls every 2 min).
async function alert(text, force) {
  if (!process.env.TELEGRAM_TOKEN || !process.env.TELEGRAM_CHAT_ID) return;
  if (!force && new Date().getUTCMinutes() % 30 >= 2) return;
  try {
    await fetch("https://api.telegram.org/bot" + process.env.TELEGRAM_TOKEN + "/sendMessage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: process.env.TELEGRAM_CHAT_ID, text, parse_mode: "HTML" }),
    });
  } catch (e) {}
}
async function handler(req, res) {
  if (!process.env.GITHUB_TOKEN) return res.status(500).send("not configured");
  const r = await gh("/actions/workflows/monitor.yml/runs?per_page=10", "GET");
  if (!r.ok) {
    await alert(r.status === 401 || r.status === 403
      ? "🆘 <b>Бот Solaris остановлен: токен GitHub не работает</b> (истёк или отозван). Проверки и команды не идут.\n" +
        "Что сделать: GitHub → Settings → Developer settings → Fine-grained tokens → solaris-webhook → Regenerate, " +
        "вставить в Vercel (solaris-webhook → Environment Variables → GITHUB_TOKEN) и нажать Redeploy."
      : "⚠️ <b>Бот Solaris: GitHub не отвечает</b> (код " + r.status + "). Проверки сейчас не запускаются. Обычно это сбой GitHub и проходит сам.");
    return res.status(502).send("github " + r.status);
  }
  const runs = ((await r.json()).workflow_runs || []);
  // a run stuck in the queue for over 15 minutes (GitHub outage) must not block checks forever
  const busy = runs.some((x) => x.status !== "completed" && Date.now() - Date.parse(x.created_at) < 15 * 60 * 1000);
  const last = runs.length ? Date.parse(runs[0].created_at) : 0;
  // Health signal for cron-job.org: when no run has succeeded for 15 minutes, answer HTTP 500,
  // so cron-job.org marks the job as failed and e-mails the owner ("notify on failure").
  const ok = runs.filter((x) => x.conclusion === "success").map((x) => Date.parse(x.created_at));
  const staleMin = Math.round((Date.now() - (ok.length ? Math.max(...ok) : 0)) / 60000);
  const stale = staleMin > 15;
  if (stale) {
    const failed = runs.slice(0, 3).filter((x) => x.conclusion === "failure").length;
    await alert("🆘 <b>Бот Solaris: " + staleMin + " мин нет успешных проверок.</b> Сообщения о свете клиентам не уходят.\n" +
      (failed >= 2 ? "Запуски падают с ошибкой: GitHub → репозиторий Solaris-monitor → Actions → последний красный запуск. Перешлите Claude, что там написано."
                   : "Запуски не стартуют или зависли: GitHub → Actions. Если там пусто — проверьте GitHub Status (githubstatus.com)."));
  }
  if (busy || Date.now() - last < 100 * 1000) return res.status(stale ? 500 : 200).send(stale ? "STALE: no successful check for " + staleMin + " min" : "skip");

  let mode = "check";
  // the token's expiry date comes back in a response header; warn two weeks ahead, once a day
  const exp = Date.parse((r.headers.get("github-authentication-token-expiration") || "").replace(" UTC", "Z").replace(" ", "T"));
  const now = new Date();
  if (exp && exp - Date.now() < 14 * 86400 * 1000 && now.getUTCHours() === 8 && now.getUTCMinutes() < 6) mode = "tokenwarn";

  const run = await gh("/actions/workflows/monitor.yml/dispatches", "POST", { ref: "main", inputs: { mode } });
  if (stale) return res.status(500).send("STALE: no successful check for " + staleMin + " min; started " + mode);
  if (!run.ok) await alert("⚠️ <b>Бот Solaris</b>: не получилось запустить проверку на GitHub (код " + run.status + "). Если повторяется — проверьте токен GitHub в Vercel.");
  return res.status(run.ok ? 200 : 502).send(run.ok ? "started " + mode : "dispatch failed " + run.status);
}
module.exports = handler;
module.exports.alert = alert;
module.exports.gh = gh;
