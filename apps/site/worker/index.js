// Serves the static site over HTTPS on the main domain, and counts downloads.
import manifest from "../../../version.json";

const REPOSITORY = "tommy44458/txin-trade";
const INSTALLERS = {
  mac: `txinTrade-${manifest.version}-mac-arm64.dmg`,
  windows: `txinTrade-${manifest.version}-win-x64.exe`,
};
const SOURCES = new Set(["home", "download"]);
const LOCALES = new Set(["en", "zh-TW"]);
// Link previews, crawlers and scripts follow links too; they are not people downloading.
const AUTOMATED = /bot|crawl|spider|slurp|preview|facebookexternalhit|curl|wget|python|headless/i;

async function countDownload(env, request, url, platform) {
  const agent = request.headers.get("user-agent") ?? "";
  if (request.method !== "GET" || !agent || AUTOMATED.test(agent)) return;
  const source = url.searchParams.get("from");
  const locale = url.searchParams.get("lang");
  await env.DB.prepare(
    `INSERT INTO site_downloads (day, platform, source, locale, country, count) VALUES (?, ?, ?, ?, ?, 1)
     ON CONFLICT (day, platform, source, locale, country) DO UPDATE SET count = count + 1`,
  ).bind(
    new Date().toISOString().slice(0, 10), platform,
    SOURCES.has(source) ? source : "other", LOCALES.has(locale) ? locale : "other",
    request.cf?.country ?? "XX",
  ).run();
}

async function recordGithubDownloads(env) {
  const response = await fetch(`https://api.github.com/repos/${REPOSITORY}/releases?per_page=100`, {
    headers: { accept: "application/vnd.github+json", "user-agent": "txintrade-site" },
  });
  if (!response.ok) throw new Error(`GitHub releases: ${response.status}`);
  const day = new Date().toISOString().slice(0, 10);
  const rows = (await response.json()).filter((release) => !release.draft).flatMap((release) =>
    release.assets.filter((asset) => /\.(dmg|zip|exe)$/.test(asset.name)).map((asset) =>
      env.DB.prepare(
        `INSERT INTO github_downloads (day, version, asset, total) VALUES (?, ?, ?, ?)
         ON CONFLICT (day, asset) DO UPDATE SET total = excluded.total`,
      ).bind(day, release.tag_name.replace(/^v/, ""), asset.name, asset.download_count)));
  if (rows.length) await env.DB.batch(rows);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    // Plain HTTP and www both go to https://txintrade.com, in one hop.
    if (url.protocol === "http:" || url.hostname === "www.txintrade.com") {
      url.protocol = "https:";
      url.hostname = "txintrade.com";
      return Response.redirect(url.toString(), 301);
    }
    const download = url.pathname.match(/^\/get\/(mac|windows)$/);
    if (download) {
      const platform = download[1];
      // A failed count must never cost a download.
      ctx.waitUntil(countDownload(env, request, url, platform).catch((error) => console.error("count download", error)));
      return new Response(null, { status: 302, headers: {
        location: `https://github.com/${REPOSITORY}/releases/latest/download/${INSTALLERS[platform]}`,
        "cache-control": "no-store",
      } });
    }
    // Pages pass through unchanged, so Cloudflare Web Analytics (cookieless) can add its beacon.
    return env.ASSETS.fetch(request);
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(recordGithubDownloads(env));
  },
};
