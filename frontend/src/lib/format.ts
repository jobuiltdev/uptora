export function relativeTime(value: string | null) {
  if (!value) return "Never";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const ranges: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["year", 31536000],
    ["month", 2592000],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [unit, size] of ranges)
    if (Math.abs(seconds) >= size)
      return formatter.format(Math.round(seconds / size), unit);
  return formatter.format(seconds, "second");
}
export function exactTime(value: string | null) {
  return value
    ? new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "—";
}
export function duration(total: number | null) {
  if (total === null) return "Ongoing";
  const days = Math.floor(total / 86400),
    hours = Math.floor((total % 86400) / 3600),
    minutes = Math.floor((total % 3600) / 60),
    seconds = total % 60;
  return (
    [
      days && `${days}d`,
      hours && `${hours}h`,
      minutes && `${minutes}m`,
      !days && !hours && seconds && `${seconds}s`,
    ]
      .filter(Boolean)
      .join(" ") || "0s"
  );
}
