import Link from "next/link";
import type { LucideIcon } from "lucide-react";
import { pct } from "./format";

import logos from "@/lib/logos.json";

const LOGOS = logos as Record<string, string>;

// Team crest: the club/national logo (football-logos.cc, 96px WebP in /public/logos) when we have it,
// otherwise a neutral grey shield with the initials. The team name is always printed next to it.
export function TeamBadge({ name, size = "sm" }: { name: string; size?: "sm" | "lg" }) {
  const px = size === "lg" ? 52 : 28;
  const file = LOGOS[name];
  if (file) {
    return <img src={`/logos/${file}`} alt="" width={px} height={px} loading="lazy" decoding="async" className={`crest${size === "lg" ? " crest-lg" : ""}`} />;
  }
  const words = name.replace(/[^\p{L}\p{N} ]/gu, "").split(" ").filter((w) => w.length > 1 && !/^(fc|ac|as|ss|sc|cf|afc|us|vfb|vfl|rc|ud|cd|sl)$/i.test(w));
  const initials = ((words[0]?.[0] ?? name[0]) + (words[1]?.[0] ?? words[0]?.[1] ?? "")).toUpperCase();
  return (
    <svg className={`crest${size === "lg" ? " crest-lg" : ""}`} width={px} height={px} viewBox="0 0 32 32" aria-hidden="true">
      <path d="M16 2 28 6v9c0 7.2-5 12.6-12 15C9 27.6 4 22.2 4 15V6z" fill="var(--crest-fill)" stroke="var(--crest-line)" strokeWidth="1.5" />
      <text x="16" y="19.5" textAnchor="middle" fontSize="9" fontWeight="700" fill="var(--crest-ink)" style={{ fontFamily: "var(--font-display)" }}>
        {initials}
      </text>
    </svg>
  );
}

export function MatchCell({ home, away, sub, href }: { home: string; away: string; sub?: React.ReactNode; href?: string }) {
  const body = (
    <span className="match-cell">
      <span className="badges">
        <TeamBadge name={home} />
        <TeamBadge name={away} />
      </span>
      <span style={{ minWidth: 0 }}>
        <b>
          {home} <span className="muted">vs</span> {away}
        </b>
        {sub && <small>{sub}</small>}
      </span>
    </span>
  );
  return href ? (
    <Link href={href} className="row-link">
      {body}
    </Link>
  ) : (
    body
  );
}

export function Empty({ icon: Icon, title, children }: { icon: LucideIcon; title: string; children?: React.ReactNode }) {
  return (
    <div className="empty">
      <Icon size={32} strokeWidth={1.5} aria-hidden="true" />
      <b>{title}</b>
      {children && <p>{children}</p>}
    </div>
  );
}

// 1X2 split as one stacked bar (2px gaps). Percentages are always printed next to it: colour is never the only cue.
export function Split1X2({ h, d, a }: { h: number | null; d: number | null; a: number | null }) {
  if (h == null || d == null || a == null) return <span className="muted">–</span>;
  return (
    <span className="stack" role="img" aria-label={`1 ${pct(h)}, X ${pct(d)}, 2 ${pct(a)}`} title={`1 ${pct(h)} · X ${pct(d)} · 2 ${pct(a)}`}>
      <span style={{ width: `${h * 100}%`, background: "var(--s1)" }} />
      <span style={{ width: `${d * 100}%`, background: "var(--s2)" }} />
      <span style={{ width: `${a * 100}%`, background: "var(--s3)" }} />
    </span>
  );
}

// Colour of a probability on rings and bars: the band (globals.css --pr-0..4), never the only cue (the figure is printed).
// "score": exact scores, whose probabilities are small (bands at 5, 10, 15, 20%)
export const probColor = (p: number, scale: "prob" | "score" = "prob") => {
  const s = scale === "score" ? 0.05 : 0.2;
  return `var(--pr-${p < s ? 0 : p < 2 * s ? 1 : p < 3 * s ? 2 : p < 4 * s ? 3 : 4})`;
};

export function Ring({ value, label, sub, top }: { value: number | null; label: string; sub?: React.ReactNode; top?: React.ReactNode }) {
  const r = 42;
  const c = 2 * Math.PI * r;
  const v = value ?? 0;
  return (
    <div className="ring">
      {top && <span className="ring-top">{top}</span>}
      <svg viewBox="0 0 100 100" role="img" aria-label={`${label}: ${pct(value, 1)}`}>
        <circle cx="50" cy="50" r={r} fill="none" stroke="var(--track)" strokeWidth="9" />
        <circle
          cx="50" cy="50" r={r} fill="none" stroke={probColor(v)} strokeWidth="9" strokeLinecap="round"
          strokeDasharray={`${Math.max(v * c - 2, 0)} ${c}`} transform="rotate(-90 50 50)"
        />
        <text x="50" y="55" textAnchor="middle" fontSize="17" fontWeight="700" fill="var(--text)" style={{ fontFamily: "var(--font-mono)" }}>
          {pct(value, 1)}
        </text>
      </svg>
      <b>{label}</b>
      {sub && <small>{sub}</small>}
    </div>
  );
}

export function Meter({ used, limit, label, hint }: { used: number; limit: number; label: string; hint?: string }) {
  const share = Math.min(used / limit, 1);
  const tone = share >= 0.9 ? "bad" : share >= 0.7 ? "warn" : "";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div className="kv" style={{ padding: 0 }}>
        <span>{label}</span>
        <span className="num">
          {used} / {limit}
        </span>
      </div>
      <div className={`meter ${tone}`} role="meter" aria-valuenow={used} aria-valuemin={0} aria-valuemax={limit} aria-label={label}>
        <span style={{ width: `${share * 100}%` }} />
      </div>
      {hint && <span className="note">{hint}</span>}
    </div>
  );
}

export function HBar({ label, p, max = 1, scale = "prob" }: { label: string; p: number; max?: number; scale?: "prob" | "score" }) {
  return (
    <div className="hbar">
      <span className="text-2">{label}</span>
      <span className="bar">
        <span style={{ width: `${Math.min(p / max, 1) * 100}%`, background: probColor(p, scale) }} />
      </span>
      <span className="num">{pct(p, 1)}</span>
    </div>
  );
}

// Speedometer-style gauge (270° arc) for one probability, the figure printed in the middle.
export function ProbGauge({ value, label, sub }: { value: number; label: string; sub?: string }) {
  const r = 50;
  const c = 2 * Math.PI * r;
  const arc = 0.75 * c;
  const v = Math.min(Math.max(value, 0), 1);
  return (
    <div className="gauge" role="img" aria-label={`${label}: ${pct(value, 1)}`}>
      <svg viewBox="0 0 120 120" aria-hidden="true">
        <circle cx="60" cy="60" r={r} fill="none" stroke="var(--track)" strokeWidth="10" strokeLinecap="round"
          strokeDasharray={`${arc} ${c}`} transform="rotate(135 60 60)" />
        <circle className="gauge-value" cx="60" cy="60" r={r} fill="none" stroke={probColor(v)} strokeWidth="10" strokeLinecap="round"
          strokeDasharray={`${Math.max(v * arc, 0.01)} ${c}`} transform="rotate(135 60 60)" style={{ "--len": v * arc, "--pc": probColor(v) } as React.CSSProperties} />
      </svg>
      <span className="gauge-text">
        <b className="num">{pct(value, 1)}</b>
        <small>{label}</small>
        {sub && <small className="gauge-sub">{sub}</small>}
      </span>
    </div>
  );
}

// Small ring without a label (profile cards): the whole-number percentage inside.
export function MiniRing({ value, label }: { value: number; label: string }) {
  const r = 40;
  const c = 2 * Math.PI * r;
  return (
    <svg className="mini-ring" viewBox="0 0 100 100" role="img" aria-label={`${label}: ${pct(value, 1)}`}>
      <circle cx="50" cy="50" r={r} fill="none" stroke="var(--track)" strokeWidth="11" />
      <circle cx="50" cy="50" r={r} fill="none" stroke={probColor(value)} strokeWidth="11" strokeLinecap="round"
        strokeDasharray={`${Math.max(value * c - 2, 0)} ${c}`} transform="rotate(-90 50 50)" />
      <text x="50" y="59" textAnchor="middle" fontSize="27" fontWeight="700" fill="var(--text)" style={{ fontFamily: "var(--font-mono)" }}>
        {pct(value, 0)}
      </text>
    </svg>
  );
}

// Inline probability bar under a table figure: the fill is p, the optional tick is the market's probability.
// Decorative (the figures are printed next to it), so hidden from screen readers; the title explains it on hover.
export function PBar({ p, mark }: { p: number; mark?: number | null }) {
  const title = `Modello ${pct(p, 1)}${mark != null ? ` · tacca: mercato ${pct(mark, 1)}` : ""}`;
  return (
    <span className="pbar" title={title} aria-hidden="true">
      <i className="pbar-fill" style={{ width: `${Math.min(p, 1) * 100}%`, "--pc": probColor(p) } as React.CSSProperties} />
      {mark != null && <i className="pbar-mark" style={{ left: `${Math.min(mark, 1) * 100}%` }} />}
    </span>
  );
}
