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

export function problemText(p: {
  title: string;
  detail?: string | null;
  errors?: { msg: string }[];
}) {
  const errors = (p.errors ?? []).map((e) => e.msg);
  return [p.detail ?? p.title, ...errors].join(" ");
}
