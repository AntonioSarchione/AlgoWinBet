"use client";

import { useMemo, useState } from "react";

export type Series = { key: string; label: string; short: string; color: string; dash?: string; points: { t: number; v: number }[] };

const W = 640;
const H = 220;
const PAD = { l: 40, r: 64, t: 12, b: 26 };
const TZ = "Europe/Rome";
const fmt = (t: number) => new Date(t).toLocaleString("it-IT", { timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

// Odds over time, one line per selection. Line style differs per series and each line is labelled at its end,
// so identity never relies on colour alone. Hover or keyboard focus shows every value at that moment.
export function OddsChart({ series, title }: { series: Series[]; title: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const { times, x, y, ticks } = useMemo(() => {
    const ts = [...new Set(series.flatMap((s) => s.points.map((p) => p.t)))].sort((a, b) => a - b);
    const vs = series.flatMap((s) => s.points.map((p) => p.v));
    const lo = Math.floor(Math.min(...vs) * 10) / 10 - 0.1;
    const hi = Math.ceil(Math.max(...vs) * 10) / 10 + 0.1;
    const t0 = ts[0];
    const t1 = ts[ts.length - 1] === t0 ? t0 + 1 : ts[ts.length - 1];
    const x = (t: number) => PAD.l + ((t - t0) / (t1 - t0)) * (W - PAD.l - PAD.r);
    const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
    const step = (hi - lo) / 4;
    return { times: ts, x, y, ticks: [0, 1, 2, 3, 4].map((k) => lo + k * step) };
  }, [series]);

  const at = (s: Series, t: number) => {
    let best: { t: number; v: number } | undefined;
    for (const p of s.points) if (p.t <= t) best = p;
    return best;
  };
  const ht = hover == null ? null : times[hover];
  // end labels: pushed apart vertically so close lines never print on top of each other
  const ends = series
    .map((s) => ({ key: s.key, y: y(s.points[s.points.length - 1].v) }))
    .sort((a, b) => a.y - b.y);
  for (let k = 1; k < ends.length; k++) ends[k].y = Math.max(ends[k].y, ends[k - 1].y + 14);
  const labelY = new Map(ends.map((e) => [e.key, e.y]));

  return (
    <div className="chart">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={title}
        tabIndex={0}
        onMouseLeave={() => setHover(null)}
        onBlur={() => setHover(null)}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") setHover((h) => Math.min((h ?? -1) + 1, times.length - 1));
          if (e.key === "ArrowLeft") setHover((h) => Math.max((h ?? times.length) - 1, 0));
        }}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - r.left) / r.width) * W;
          let k = 0;
          times.forEach((t, i) => {
            if (Math.abs(x(t) - px) < Math.abs(x(times[k]) - px)) k = i;
          });
          setHover(k);
        }}
      >
        {ticks.map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeWidth="1" />
            <text x={PAD.l - 8} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)" style={{ fontFamily: "var(--font-mono)" }}>
              {v.toFixed(2)}
            </text>
          </g>
        ))}
        {[times[0], times[times.length - 1]].map((t, i) => (
          <text key={i} x={x(t)} y={H - 6} textAnchor={i ? "end" : "start"} fontSize="11" fill="var(--muted)">
            {fmt(t)}
          </text>
        ))}
        {series.map((s) => {
          const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
          const last = s.points[s.points.length - 1];
          return (
            <g key={s.key}>
              <path d={d} fill="none" stroke={s.color} strokeWidth="2" strokeDasharray={s.dash} strokeLinejoin="round" strokeLinecap="round" />
              <circle cx={x(last.t)} cy={y(last.v)} r="4" fill={s.color} stroke="var(--card)" strokeWidth="2" />
              <text x={x(last.t) + 8} y={(labelY.get(s.key) ?? y(last.v)) + 4} fontSize="12" fill="var(--text-2)" style={{ fontFamily: "var(--font-mono)" }}>
                {s.short} {last.v.toFixed(2)}
              </text>
            </g>
          );
        })}
        {ht != null && (
          <g>
            <line x1={x(ht)} x2={x(ht)} y1={PAD.t} y2={H - PAD.b} stroke="var(--line-strong)" strokeWidth="1" />
            {series.map((s) => {
              const p = at(s, ht);
              return p ? <circle key={s.key} cx={x(ht)} cy={y(p.v)} r="5" fill={s.color} stroke="var(--card)" strokeWidth="2" /> : null;
            })}
          </g>
        )}
      </svg>
      {ht != null && (
        <div className="tip" style={{ left: `${(x(ht) / W) * 100}%`, top: 12 }}>
          <div className="muted" style={{ marginBottom: 4 }}>{fmt(ht)}</div>
          {series.map((s) => {
            const p = at(s, ht);
            return (
              <div key={s.key} className="kv" style={{ padding: "1px 0" }}>
                <span>{s.label}</span>
                <span className="num">{p ? p.v.toFixed(2) : "–"}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
