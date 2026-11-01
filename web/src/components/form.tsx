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
