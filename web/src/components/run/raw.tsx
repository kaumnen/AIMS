import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef } from "react";

import type { Event } from "@/api/events";
import { eventQuery, rawEventsQuery } from "@/api/run";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { summary, typeGroups } from "@/lib/event-text";
import { cn } from "@/lib/utils";

export interface RawSearch {
  group?: string;
  q?: string;
  seq?: number;
}

const ANY = "any";
const clock = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  fractionalSecondDigits: 3,
  hour12: false,
});

export const RAW_SEARCH_ID = "raw-search";

export function RawEvents({
  runId,
  search,
  onChange,
}: {
  runId: string;
  search: RawSearch;
  onChange: (update: (prev: RawSearch) => RawSearch) => void;
}) {
  const types = typeGroups.find((g) => g.name === search.group)?.types;
  const list = useInfiniteQuery(
    rawEventsQuery(runId, { ...(types ? { types } : {}), ...(search.q ? { q: search.q } : {}) }),
  );
  const events = list.data?.pages.flatMap((p) => p.items) ?? [];
  const set = (key: keyof RawSearch, value: string | number | undefined) =>
    onChange((prev) => {
      const next = { ...prev };
      if (value === undefined) delete next[key];
      else (next as Record<string, unknown>)[key] = value;
      return next;
    });
  const items = [
    { value: ANY, label: "Any type" },
    ...typeGroups.map((g) => ({ value: g.name, label: g.name })),
  ];
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Select
          items={items}
          value={search.group ?? ANY}
          onValueChange={(v) => set("group", v === null || v === ANY ? undefined : String(v))}
        >
          <SelectTrigger size="sm" aria-label="Type" className="min-w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false} align="start">
            {items.map((i) => (
              <SelectItem key={i.value} value={i.value}>
                {i.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          id={RAW_SEARCH_ID}
          key={search.q ?? ""}
          aria-label="Search events"
          placeholder="Search events"
          defaultValue={search.q ?? ""}
          className="h-7 w-72 text-sm md:text-sm"
          onKeyDown={(e) => {
            if (e.key === "Enter") set("q", e.currentTarget.value.trim() || undefined);
            if (e.key === "Escape") e.currentTarget.blur();
          }}
        />
        <span className="text-muted-foreground ml-auto tabular-nums">
          {list.isSuccess ? `${events.length}${list.hasNextPage ? "+" : ""} events` : ""}
        </span>
      </div>
      <div className="flex min-h-0 flex-1 gap-3 max-md:flex-col">
        {list.isError ? (
          <p className="text-muted-foreground">Events did not load.</p>
        ) : list.isSuccess && events.length === 0 ? (
          <p className="text-muted-foreground">No events match.</p>
        ) : (
          <EventList
            events={events}
            selected={search.seq}
            onSelect={(seq) => set("seq", seq)}
            onNearEnd={() => {
              if (list.hasNextPage && !list.isFetchingNextPage) void list.fetchNextPage();
            }}
          />
        )}
        {search.seq !== undefined && (
          <EventDetail
            runId={runId}
            seq={search.seq}
            loaded={events.find((e) => e.seq === search.seq)}
          />
        )}
      </div>
    </div>
  );
}

function EventList({
  events,
  selected,
  onSelect,
  onNearEnd,
}: {
  events: readonly Event[];
  selected: number | undefined;
  onSelect: (seq: number) => void;
  onNearEnd: () => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  // The compiler leaves this component alone; rows get plain values, so they still memoize.
  // oxlint-disable-next-line react/incompatible-library
  const v = useVirtualizer({
    count: events.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => 24,
    overscan: 20,
    getItemKey: (i) => events[i]?.seq ?? i,
  });
  const items = v.getVirtualItems();
  const last = items.at(-1)?.index ?? -1;
  useEffect(() => {
    if (last >= events.length - 40) onNearEnd();
  }, [last, events.length, onNearEnd]);
  return (
    <div ref={scroller} className="min-h-0 flex-1 overflow-auto rounded-sm border">
      <div
        role="listbox"
        aria-label="Events"
        className="relative"
        style={{ height: v.getTotalSize() }}
      >
        {items.map((item) => {
          const e = events[item.index];
          if (e === undefined) return null;
          return (
            <EventRow
              key={item.key}
              event={e}
              top={item.start}
              active={e.seq === selected}
              onSelect={onSelect}
            />
          );
        })}
      </div>
    </div>
  );
}

function EventRow({
  event,
  top,
  active,
  onSelect,
}: {
  event: Event;
  top: number;
  active: boolean;
  onSelect: (seq: number) => void;
}) {
  return (
    <div
      role="option"
      aria-selected={active}
      onClick={() => onSelect(event.seq)}
      className={cn(
        "absolute inset-x-0 top-0 grid h-6 cursor-default grid-cols-[4rem_6.5rem_5rem_8.5rem_minmax(0,1fr)] items-center gap-x-3 px-2 font-mono text-xs",
        active ? "bg-accent" : "hover:bg-accent/60",
        event.type === "monitor.flag" && "text-flag",
      )}
      style={{ transform: `translateY(${top}px)` }}
    >
      <span className="text-muted-foreground text-right tabular-nums">{event.seq}</span>
      <span className="text-muted-foreground tabular-nums">{clock.format(new Date(event.ts))}</span>
      <span className="text-muted-foreground truncate">{event.agent_id}</span>
      <span className="truncate">{event.type}</span>
      <span className="truncate">{summary(event)}</span>
    </div>
  );
}

function EventDetail({
  runId,
  seq,
  loaded,
}: {
  runId: string;
  seq: number;
  loaded: Event | undefined;
}) {
  const q = useQuery({ ...eventQuery(runId, seq), enabled: loaded === undefined });
  const event = loaded ?? q.data;
  return (
    <aside className="min-h-0 overflow-auto rounded-sm border p-2 max-md:max-h-80 md:w-[28rem] md:shrink-0">
      {event ? (
        <>
          <p className="mb-1 font-medium">
            {event.type}{" "}
            <span className="text-muted-foreground font-normal tabular-nums">#{event.seq}</span>
          </p>
          <pre className="font-mono text-xs break-words whitespace-pre-wrap">
            {JSON.stringify(event.data, null, 2)}
          </pre>
        </>
      ) : (
        <p className="text-muted-foreground">{q.isError ? `No event ${seq}.` : "Loading event"}</p>
      )}
    </aside>
  );
}
