"use client";

import Link, { useLinkStatus } from "next/link";
import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, LoaderCircle } from "lucide-react";

export type ChipItem = { key: string; href: string; label: string };

// While the page for a clicked choice loads, the chip shows a small spinner (the rest of the page stays as it is).
function Pending() {
  const { pending } = useLinkStatus();
  return pending ? <LoaderCircle size={13} className="spin chip-pending" aria-hidden="true" /> : null;
}

// One row of choices as links (the view stays shareable and works without JavaScript).
// "seg": a segmented control for a few choices; "scroll": one line that scrolls sideways, with arrows when it overflows and
// the chosen chip brought into view, so dozens of markets never pile up as a wall of chips.
export function ChipRow({ label, items, active, variant = "scroll" }: { label: string; items: ChipItem[]; active: string; variant?: "seg" | "scroll" }) {
  const row = useRef<HTMLDivElement>(null);
  const [edge, setEdge] = useState({ left: false, right: false });

  useEffect(() => {
    const el = row.current;
    if (!el || variant !== "scroll") return;
    const update = () => setEdge({ left: el.scrollLeft > 4, right: el.scrollLeft + el.clientWidth < el.scrollWidth - 4 });
    const on = el.querySelector<HTMLElement>('[aria-current="true"]');
    if (on) el.scrollLeft = Math.max(on.offsetLeft - el.clientWidth / 2 + on.offsetWidth / 2, 0);
    update();
    el.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      el.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [variant, active, items.length]);

  const scroll = (dir: number) => row.current?.scrollBy({ left: dir * row.current.clientWidth * 0.7, behavior: "smooth" });
  const links = items.map((x) => (
    <Link key={x.key} href={x.href} scroll={false} aria-current={x.key === active ? "true" : undefined}>
      {x.label}
      <Pending />
    </Link>
  ));

  return (
    <div className="qrow">
      <span className="qrow-label">{label}</span>
      {variant === "seg" ? (
        <nav className="seg" aria-label={label}>{links}</nav>
      ) : (
        <div className={`qscroll${edge.left ? " at-left" : ""}${edge.right ? " at-right" : ""}`}>
          {edge.left && (
            <button type="button" className="qscroll-btn left" onClick={() => scroll(-1)} aria-label={`${label}: scorri a sinistra`}>
              <ChevronLeft size={16} aria-hidden="true" />
            </button>
          )}
          <nav ref={row} className="qchips" aria-label={label}>{links}</nav>
          {edge.right && (
            <button type="button" className="qscroll-btn right" onClick={() => scroll(1)} aria-label={`${label}: scorri a destra`}>
              <ChevronRight size={16} aria-hidden="true" />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
