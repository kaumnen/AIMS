import { type KeyboardEvent, type PointerEvent, useRef, useState } from "react";

import { cn } from "@/lib/utils";

// The narrowest a side column goes, and the room the center keeps.
const MIN = 200;
const CENTER_MIN = 360;
const STEP = 16;

// A width between the narrowest and the most the center can give up from where the drag began.
const clamp = (w: number, from: number, room: number) =>
  Math.round(Math.min(Math.max(w, MIN), Math.max(from + room, MIN)));

const read = (key: string): number | null => {
  try {
    const n = Number(localStorage.getItem(key));
    return Number.isFinite(n) && n >= MIN ? n : null;
  } catch {
    return null;
  }
};

const write = (key: string, width: number | null) => {
  try {
    if (width === null) localStorage.removeItem(key);
    else localStorage.setItem(key, String(Math.round(width)));
  } catch {
    // private windows may refuse storage; the width still holds for this page
  }
};

// A column width this browser remembers; null keeps the layout's own.
export function useStoredWidth(key: string) {
  const [width, setWidth] = useState(() => read(key));
  const set = (w: number | null, keep: boolean) => {
    setWidth(w);
    if (keep) write(key, w);
  };
  return [width, set] as const;
}

// A handle between a side column and the center of the same grid. `side` says where the column
// is: "start" for one on the left, "end" for one on the right. The grid's center has data-center.
export function ResizeHandle({
  label,
  side,
  share,
  width,
  fallback,
  onWidth,
  className,
}: {
  label: string;
  side: "start" | "end";
  // the most of the grid's width the column may take, as the layout's own cap sets it
  share: number;
  width: number | null;
  // the column's own width while no handle has set one, for assistive tech
  fallback: number;
  onWidth: (width: number | null, keep: boolean) => void;
  className?: string;
}) {
  const drag = useRef<{ x: number; w: number; room: number } | null>(null);
  const [active, setActive] = useState(false);
  const sign = side === "start" ? 1 : -1;

  // the column's width now and how far it may grow before the center gets too narrow
  const measure = (el: HTMLElement) => {
    const grid = el.parentElement?.getBoundingClientRect();
    const own = el.getBoundingClientRect();
    const center = el.parentElement?.querySelector("[data-center]")?.getBoundingClientRect();
    if (!grid) return null;
    const w = side === "start" ? own.left - grid.left : grid.right - own.right;
    const room = Math.max(0, (center?.width ?? 0) - CENTER_MIN);
    return { w, room: Math.min(room, grid.width * share - w) };
  };

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const m = measure(e.currentTarget);
    if (!m) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, ...m };
    setActive(true);
  };
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    onWidth(clamp(d.w + sign * (e.clientX - d.x), d.w, d.room), false);
  };
  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    setActive(false);
    onWidth(clamp(d.w + sign * (e.clientX - d.x), d.w, d.room), true);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const move = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (move === 0) return;
    const m = measure(e.currentTarget);
    if (!m) return;
    e.preventDefault();
    onWidth(clamp(m.w + sign * move * STEP, m.w, m.room), true);
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width ?? fallback}
      aria-valuemin={MIN}
      tabIndex={0}
      title="Drag to resize, double-click to reset"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onDoubleClick={() => onWidth(null, true)}
      onKeyDown={onKeyDown}
      className={cn(
        "group flex cursor-col-resize touch-none justify-center outline-none",
        className,
      )}
    >
      <div
        className={cn(
          "w-0.5 rounded-full",
          active ? "bg-ring" : "group-hover:bg-ring/60 group-focus-visible:bg-ring",
        )}
      />
    </div>
  );
}
