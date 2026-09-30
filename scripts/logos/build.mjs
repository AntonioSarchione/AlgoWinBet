// Download the matched 256px PNGs (politely, cached) and write 96px WebP crests + the name -> file map for the web app.
import fs from "node:fs";
import path from "node:path";
import sharp from "sharp";

const [, , webDir] = process.argv;
const index = JSON.parse(fs.readFileSync("index.json", "utf8"));
const matches = JSON.parse(fs.readFileSync("matches.json", "utf8"));
const outDir = path.join(webDir, "public", "logos");
const cache = "png-cache";
fs.mkdirSync(outDir, { recursive: true });
fs.mkdirSync(cache, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const map = {};
let bytes = 0;
for (const [team, key] of Object.entries(matches)) {
  const e = index[key];
  const file = `${e.country}-${e.id}.webp`;
  const png = path.join(cache, `${e.country}-${e.id}.png`);
  if (!fs.existsSync(png)) {
    const r = await fetch(e.png256, { headers: { "User-Agent": "Mozilla/5.0 (AlgoWinBet personal dashboard)" } });
    if (!r.ok) { console.error("HTTP", r.status, team, e.png256); continue; }
    fs.writeFileSync(png, Buffer.from(await r.arrayBuffer()));
    await sleep(400);
  }
  const target = path.join(outDir, file);
  if (!fs.existsSync(target)) {
    await sharp(png).resize(96, 96, { fit: "contain", background: { r: 0, g: 0, b: 0, alpha: 0 } }).webp({ quality: 82, effort: 6 }).toFile(target);
  }
  bytes += fs.statSync(target).size;
  map[team] = file;
}
fs.writeFileSync(path.join(webDir, "lib", "logos.json"), JSON.stringify(map, Object.keys(map).sort(), 1) + "\n");
console.log(`loghi: ${Object.keys(map).length}, peso totale ${(bytes / 1024).toFixed(0)} KB`);
