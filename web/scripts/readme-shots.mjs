// Screenshots of the dashboard for the README (docs/screenshots/*.png), taken by the owner on the owner's PC.
//
//   node web/scripts/readme-shots.mjs [https://algowinbet.vercel.app]
//
// A visible Chrome (or Edge) window opens on the login page with a fresh throwaway profile (no cookies, no saved
// sessions): the owner types the password in that window. The script never reads, receives or stores the password; it
// waits until the login is done, then visits the pages and saves one screenshot each. The temporary profile is deleted
// at the end. Only Node (24+, built-in WebSocket) and an installed Chrome/Edge are needed.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SITE = (process.argv[2] ?? "https://algowinbet.vercel.app").replace(/\/$/, "");
const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "docs", "screenshots");
const PORT = 9333;
// Readable on GitHub: the README column is about 1000 px wide, so a whole page shrinks to an unreadable thumbnail. Each
// shot is one or a few sections (the cards around the given headings, by id or by the start of their text), rendered at
// twice the pixel density.
const WIDTH = 1280, HEIGHT = 900, SCALE = 2, MAX_H = 1000, PAD = 10;
const SHOTS = [
  ["home-schedina", "/", ["#hero-title", "#funnel-title"]],
  ["home-perche", "/", ["#slip-title", "#why-title"]],
  ["home-opportunita", "/", ["#opp-title"]],
  ["partita-1x2", "partita", ["#match-title", "Probabilità 1X2"]],
  ["registro", "/registro", ["#reg-res", "#reg-mkt"]],
  ["registro-criterio", "/registro", ["#crit-title"]],
  ["qualita", "/qualita", ["Qualità delle probabilità"]],
  ["sistema", "/sistema", ["Budget richieste API"]],
];

// in the page: the union of the cards that contain the given headings, in document coordinates
const RECT = (keys) => `(() => {
  const keys = ${JSON.stringify(keys)};
  const heads = [...document.querySelectorAll("h1, h2")];
  const boxes = keys.map((k) => {
    const h = k.startsWith("#") ? document.querySelector(k) : heads.find((e) => e.textContent.trim().startsWith(k));
    const box = h && (h.closest(".card, section, header") ?? h.parentElement);
    return box ? box.getBoundingClientRect() : null;
  }).filter(Boolean);
  if (!boxes.length) return null;
  const x = Math.min(...boxes.map((b) => b.left)), y = Math.min(...boxes.map((b) => b.top));
  const r = Math.max(...boxes.map((b) => b.right)), btm = Math.max(...boxes.map((b) => b.bottom));
  return { x: x + scrollX, y: y + scrollY, width: r - x, height: btm - y };
})()`;

const BROWSERS = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
];
const exe = BROWSERS.find((p) => existsSync(p));
if (!exe) throw new Error("Chrome o Edge non trovato");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = mkdtempSync(join(tmpdir(), "algowinbet-shots-"));
const browser = spawn(exe, [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", `--window-size=${WIDTH},${HEIGHT + 140}`, `${SITE}/login`,
], { stdio: "ignore" });

async function target() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* browser still starting */ }
    await sleep(200);
  }
  throw new Error("il browser non risponde");
}

const ws = new WebSocket(await target());
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let seq = 0;
const pending = new Map();
const waiters = [];
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) {
    const { ok, ko } = pending.get(m.id);
    pending.delete(m.id);
    m.error ? ko(new Error(m.error.message)) : ok(m.result);
  } else if (m.method) {
    for (const w of waiters.filter((w) => w.method === m.method)) { waiters.splice(waiters.indexOf(w), 1); w.ok(m.params); }
  }
});
const send = (method, params = {}) => new Promise((ok, ko) => { pending.set(++seq, { ok, ko }); ws.send(JSON.stringify({ id: seq, method, params })); });
const once = (method, ms = 30000) => Promise.race([new Promise((ok) => waiters.push({ method, ok })), sleep(ms)]);
const evaluate = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true })).result.value;

try {
  await send("Page.enable");
  console.log("Accedi nella finestra del browser che si è aperta: aspetto il login (massimo 5 minuti)...");
  const until = Date.now() + 5 * 60_000;
  // logged in = on the site and off the login page (the window starts on about:blank, which is not a login)
  const origin = new URL(SITE).origin;
  const done = `location.origin === ${JSON.stringify(origin)} && location.pathname !== "/login" && !!document.querySelector("h1")`;
  while (!(await evaluate(done).catch(() => false))) {
    if (Date.now() > until) throw new Error("login non fatto entro 5 minuti");
    await sleep(1000);
  }
  await send("Emulation.setDeviceMetricsOverride", { width: WIDTH, height: HEIGHT, deviceScaleFactor: SCALE, mobile: false });
  mkdirSync(OUT, { recursive: true });
  let at = null, match = null;
  for (const [name, path, keys] of SHOTS) {
    let url = path === "partita" ? match : SITE + path;
    if (path === "partita" && !match) {
      await send("Page.navigate", { url: SITE + "/" });
      await once("Page.loadEventFired");
      await sleep(2500);
      at = SITE + "/";
      const href = await evaluate(`document.querySelector('a[href^="/partita/"]')?.getAttribute('href') ?? null`);
      if (!href) { console.log(`${name}: nessuna partita collegata, saltata`); continue; }
      url = match = SITE + href;
    }
    if (url !== at) {
      await send("Page.navigate", { url });
      await once("Page.loadEventFired");
      await sleep(3000); // streamed sections and charts
      at = url;
    }
    const r = await evaluate(RECT(keys));
    if (!r) { console.log(`${name}: sezioni non trovate (${keys.join(", ")}), saltata`); continue; }
    const clip = { x: Math.max(0, r.x - PAD), y: Math.max(0, r.y - PAD), width: Math.min(WIDTH - Math.max(0, r.x - PAD), r.width + 2 * PAD), height: Math.min(MAX_H, r.height + 2 * PAD), scale: 1 };
    const { data } = await send("Page.captureScreenshot", { format: "png", clip, captureBeyondViewport: true });
    writeFileSync(join(OUT, `${name}.png`), Buffer.from(data, "base64"));
    console.log(`${name}: ${url.replace(SITE, "") || "/"} ${Math.round(clip.width)}×${Math.round(clip.height)}`);
  }
  console.log(`Fatto: ${OUT}`);
} finally {
  ws.close();
  browser.kill();
  await sleep(1000);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
}
