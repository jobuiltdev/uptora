"use client";

import { useQuery } from "@tanstack/react-query";
import { duration, exactTime } from "@/lib/format";
import type { PublicIncidentReport } from "@/lib/types";

async function loadReport(token: string) {
  const response = await fetch(
    `/api/public/incident-shares/${encodeURIComponent(token)}`,
    {
      cache: "no-store",
    },
  );
  if (!response.ok) throw new Error("unavailable");
  return (await response.json()) as PublicIncidentReport;
}

export function IncidentReportView({
  report,
  token,
}: {
  report: PublicIncidentReport;
  token: string;
}) {
  const resolved = report.status === "RESOLVED";
  return (
    <main className="mx-auto min-h-screen max-w-5xl px-5 py-10 md:py-16">
      <header className="mb-8 flex flex-wrap items-start justify-between gap-5">
        <div>
          <p className="eyebrow">Uptora incident proof</p>
          <h1 className="page-title mt-2">{report.website_name}</h1>
          <a
            className="muted mt-2 block hover:underline"
            href={report.website_url}
          >
            {report.website_url}
          </a>
        </div>
        <span
          className={`rounded-full px-4 py-2 text-sm font-semibold ${resolved ? "bg-emerald-100 text-emerald-900" : "bg-red-100 text-red-900"}`}
        >
          {resolved ? "Resolved incident" : "Ongoing incident"}
        </span>
      </header>
      <section className="grid gap-4 md:grid-cols-4">
        <article className="card p-5">
          <p className="muted text-sm">Monitor</p>
          <p className="font-semibold mt-2">{report.monitor_type}</p>
        </article>
        <article className="card p-5">
          <p className="muted text-sm">Started</p>
          <p className="font-semibold mt-2">{exactTime(report.started_at)}</p>
        </article>
        <article className="card p-5">
          <p className="muted text-sm">Recovered</p>
          <p className="font-semibold mt-2">{exactTime(report.resolved_at)}</p>
        </article>
        <article className="card p-5">
          <p className="muted text-sm">Duration</p>
          <p className="font-semibold mt-2">
            {duration(report.duration_seconds)}
          </p>
        </article>
      </section>
      <section className="card p-6 mt-6">
        <h2 className="font-semibold text-lg">What Uptora observed</h2>
        <p className="mt-3">{report.failure_summary}</p>
        {report.status_code && (
          <p className="muted text-sm mt-2">
            Latest status code: {report.status_code}
          </p>
        )}
        <p className="muted text-sm mt-4">
          {report.failure_count} failed checks · {report.recovery_count}{" "}
          recovery confirmations
        </p>
        <p className="muted text-xs mt-3">
          This is factual monitoring evidence, not a causal diagnosis.
        </p>
      </section>
      <section className="card p-6 mt-6">
        <h2 className="font-semibold text-lg">Incident timeline</h2>
        <ol className="mt-5 grid gap-4">
          {report.timeline.map((entry, index) => (
            <li
              className="border-l-2 border-slate-200 pl-4"
              key={`${entry.occurred_at}-${index}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <strong>{entry.label}</strong>
                <time className="muted text-sm">
                  {exactTime(entry.occurred_at)}
                </time>
              </div>
              <p className="mt-1 text-sm">{entry.summary}</p>
            </li>
          ))}
        </ol>
        {report.timeline_total_count > report.timeline.length && (
          <p className="muted text-xs mt-5">
            Showing a bounded selection of {report.timeline_total_count} checks.
          </p>
        )}
      </section>
      {report.evidence_available && (
        <section className="card p-6 mt-6">
          <h2 className="font-semibold text-lg">Screenshot evidence</h2>
          <p className="muted text-sm mt-1">Captured during a failed check.</p>
          {/* The token-scoped proxy never reveals a storage path or result id. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            className="mt-5 w-full rounded-lg border border-slate-200"
            src={`/api/public/incident-shares/${encodeURIComponent(token)}/evidence`}
            alt="Screenshot captured when the incident check failed"
          />
        </section>
      )}
      <footer className="muted py-8 text-center text-sm">
        Report provided by Uptora
      </footer>
    </main>
  );
}

export function PublicIncidentReport({ token }: { token: string }) {
  const query = useQuery({
    queryKey: ["public-incident-report", token],
    queryFn: () => loadReport(token),
    retry: false,
  });
  if (query.isLoading)
    return (
      <main className="mx-auto max-w-3xl p-10 muted">
        Loading incident report…
      </main>
    );
  if (!query.data)
    return (
      <main className="mx-auto max-w-3xl p-10 text-center">
        <p className="eyebrow">Uptora incident proof</p>
        <h1 className="page-title mt-3">Report unavailable</h1>
        <p className="muted mt-3">
          This report link is unavailable. Ask the sender for a current link.
        </p>
      </main>
    );
  return <IncidentReportView report={query.data} token={token} />;
}
