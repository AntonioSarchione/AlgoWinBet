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
const WIDTH = 1440, HEIGHT = 900;
const PAGES = [
  ["home", "/"],
  ["schedina", "/schedina"],
  ["partita", null], // the first match linked from the home page
  ["palinsesto", "/palinsesto"],
  ["registro", "/registro"],
  ["qualita", "/qualita"],
  ["sistema", "/sistema"],
];

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
  while ((await evaluate("location.pathname").catch(() => "/login")) === "/login") {
    if (Date.now() > until) throw new Error("login non fatto entro 5 minuti");
    await sleep(1000);
  }
  await send("Emulation.setDeviceMetricsOverride", { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false });
  mkdirSync(OUT, { recursive: true });
  for (const [name, path] of PAGES) {
    let url = path && SITE + path;
    if (!url) {
      await send("Page.navigate", { url: SITE + "/" });
      await once("Page.loadEventFired");
      await sleep(2500);
      const href = await evaluate(`document.querySelector('a[href^="/partita/"]')?.getAttribute('href') ?? null`);
      if (!href) { console.log(`${name}: nessuna partita collegata, saltata`); continue; }
      url = SITE + href;
    }
    await send("Page.navigate", { url });
    await once("Page.loadEventFired");
    await sleep(3000); // streamed sections and charts
    await evaluate("window.scrollTo(0, 0)");
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(OUT, `${name}.png`), Buffer.from(data, "base64"));
    console.log(`${name}: ${url.replace(SITE, "")}`);
  }
  console.log(`Fatto: ${OUT}`);
} finally {
  ws.close();
  browser.kill();
  await sleep(1000);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
}
