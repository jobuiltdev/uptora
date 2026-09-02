import type { CheckResult } from "@/lib/types";

export function RunOutcome({ result }: { result: CheckResult }) {
  return (
    <section
      className={`card border-l-4 p-5 ${result.is_success ? "border-l-emerald-600" : "border-l-red-600"}`}
      role="status"
    >
      <h2 className="font-semibold">
        Target check {result.is_success ? "passed" : "failed"}
      </h2>
      <p className="muted mt-1 text-sm">
        {result.is_success
          ? `Response received in ${result.response_time_ms ?? "—"} ms`
          : (result.error_message ??
            result.error_type ??
            "The target did not pass this check.")}
      </p>
    </section>
  );
}
