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

module.exports = async (req, res) => {
  if (!process.env.GITHUB_TOKEN) return res.status(500).send("not configured");
  const r = await gh("/actions/workflows/monitor.yml/runs?per_page=5", "GET");
  if (!r.ok) return res.status(502).send("github " + r.status);
  const runs = ((await r.json()).workflow_runs || []);
  const busy = runs.some((x) => x.status !== "completed");
  const last = runs.length ? Date.parse(runs[0].created_at) : 0;
  if (busy || Date.now() - last < 100 * 1000) return res.status(200).send("skip");

  let mode = "check";
  // the token's expiry date comes back in a response header; warn two weeks ahead, once a day
  const exp = Date.parse((r.headers.get("github-authentication-token-expiration") || "").replace(" UTC", "Z").replace(" ", "T"));
  const now = new Date();
  if (exp && exp - Date.now() < 14 * 86400 * 1000 && now.getUTCHours() === 8 && now.getUTCMinutes() < 6) mode = "tokenwarn";

  const run = await gh("/actions/workflows/monitor.yml/dispatches", "POST", { ref: "main", inputs: { mode } });
  return res.status(run.ok ? 200 : 502).send(run.ok ? "started " + mode : "dispatch failed " + run.status);
};
