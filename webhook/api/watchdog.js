// Daily watchdog (Vercel Cron, see vercel.json): catches the case when the external timer (cron-job.org) has stopped
// calling /api/tick, so no checks run and nobody else would notice. Reads the last workflow runs; if the last run
// is more than 30 minutes old, writes to the staff Telegram group. Reads only, starts nothing.
const tick = require("./tick.js");

module.exports = async (req, res) => {
  const r = await tick.gh("/actions/workflows/monitor.yml/runs?per_page=5", "GET");
  if (!r.ok) {
    await tick.alert("⚠️ <b>Бот Solaris (суточная проверка)</b>: GitHub не отвечает или токен не работает (код " + r.status + ").", true);
    return res.status(502).send("github " + r.status);
  }
  const runs = ((await r.json()).workflow_runs || []);
  // Проверка связи Vercel -> Telegram: ?test=<первые 7 знаков коммита последнего запуска>
  const q = (req.query && req.query.test) || "";
  if (q && runs.length && q === String(runs[0].head_sha).slice(0, 7)) {
    const ok = process.env.TELEGRAM_TOKEN && process.env.TELEGRAM_CHAT_ID;
    if (ok) await tick.alert("✅ Проверка связи: Vercel умеет писать в эту группу. Если бот остановится, предупреждение придёт сюда.", true);
    return res.status(ok ? 200 : 500).send(ok ? "test sent" : "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set");
  }
  const lastMin = Math.round((Date.now() - (runs.length ? Date.parse(runs[0].created_at) : 0)) / 60000);
  if (lastMin > 30) {
    await tick.alert("🆘 <b>Бот Solaris: проверок не было " + lastMin + " мин.</b> Похоже, остановился таймер cron-job.org " +
      "(он раз в 2 минуты запускает проверки). Сообщения о свете клиентам не уходят.\n" +
      "Что сделать: cron-job.org → задание «Solaris» → включено ли оно и что в истории запусков.", true);
    return res.status(500).send("stale " + lastMin);
  }
  const exp = r.headers.get("github-authentication-token-expiration") || "no expiration";
  return res.status(200).send("ok, last run " + lastMin + " min ago; github token expires: " + exp);
};
