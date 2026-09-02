import {
  CircleAlert,
  CircleCheck,
  CircleDashed,
  PauseCircle,
  TriangleAlert,
} from "lucide-react";
import type { Health } from "@/lib/types";
const styles: Record<Health, string> = {
  HEALTHY: "bg-emerald-50 text-emerald-800 border-emerald-200",
  PROBLEM: "bg-red-50 text-red-800 border-red-200",
  DEGRADED: "bg-amber-50 text-amber-800 border-amber-200",
  UNKNOWN: "bg-slate-50 text-slate-700 border-slate-200",
  PAUSED: "bg-slate-50 text-slate-700 border-slate-200",
  DISABLED: "bg-slate-50 text-slate-700 border-slate-200",
};
export function Status({ value }: { value: Health }) {
  const Icon =
    value === "HEALTHY"
      ? CircleCheck
      : value === "PROBLEM"
        ? CircleAlert
        : value === "DEGRADED"
          ? TriangleAlert
          : value === "UNKNOWN"
            ? CircleDashed
            : PauseCircle;
  const label =
    value === "DEGRADED"
      ? "Pending confirmation"
      : value[0] + value.slice(1).toLowerCase();
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-semibold ${styles[value]}`}
    >
      <Icon size={14} />
      {label}
    </span>
  );
}
