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
  const lastMin = Math.round((Date.now() - (runs.length ? Date.parse(runs[0].created_at) : 0)) / 60000);
  if (lastMin > 30) {
    await tick.alert("🆘 <b>Бот Solaris: проверок не было " + lastMin + " мин.</b> Похоже, остановился таймер cron-job.org " +
      "(он раз в 2 минуты запускает проверки). Сообщения о свете клиентам не уходят.\n" +
      "Что сделать: cron-job.org → задание «Solaris» → включено ли оно и что в истории запусков.", true);
    return res.status(500).send("stale " + lastMin);
  }
  return res.status(200).send("ok, last run " + lastMin + " min ago");
};
