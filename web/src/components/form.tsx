import { useState } from "react";

import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

// The value of a select that leaves the choice to the config.
export const DEFAULT = "";

export function Field({
  label,
  children,
  className,
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("flex flex-col gap-1", className)}>
      <span className="text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

export function Pick({
  label,
  value,
  items,
  onChange,
}: {
  label: string;
  value: string;
  items: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <Select items={items} value={value} onValueChange={(v) => onChange(String(v ?? DEFAULT))}>
      <SelectTrigger size="sm" aria-label={label} className="w-full">
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} align="start">
        {items.map((i) => (
          <SelectItem key={i.value || "default"} value={i.value}>
            {i.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

// A whole number from 1 to `max`. The field can be emptied while typing a new number: it
// reports 1 until a number is in, and shows the number it reports once it loses focus.
export function CountInput({
  value,
  max,
  onChange,
  disabled,
}: {
  value: number;
  max: number;
  onChange: (n: number) => void;
  disabled?: boolean;
}) {
  const [typed, setTyped] = useState<string | null>(null);
  return (
    <Input
      type="number"
      min={1}
      max={max}
      value={typed ?? value}
      disabled={disabled}
      className="h-7 md:text-sm"
      onChange={(e) => {
        const raw = e.currentTarget.value;
        const n = Math.round(Number(raw));
        setTyped(raw);
        onChange(raw.trim() !== "" && Number.isFinite(n) ? Math.min(max, Math.max(1, n)) : 1);
      }}
      onBlur={() => setTyped(null)}
    />
  );
}

export function problemText(p: {
  title: string;
  detail?: string | null;
  errors?: { msg: string }[];
}) {
  const errors = (p.errors ?? []).map((e) => e.msg);
  return [p.detail ?? p.title, ...errors].join(" ");
}

// What the control plane's detail of a save refused with 412 says happened to the file.
const CONFLICTS: Record<string, string> = {
  "the file changed since it was read": "changed on disk",
  "the file does not exist": "was deleted on disk",
  "the file exists": "was created on disk",
};

export function conflictText(name: string, detail: string): string {
  const what = CONFLICTS[detail];
  return what
    ? `${name} ${what} since you opened it.`
    : `${name} changed on disk since you opened it (${detail}).`;
}
