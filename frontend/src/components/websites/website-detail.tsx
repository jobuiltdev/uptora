"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import type {
  CheckResult,
  Incident,
  Monitor,
  Page,
  Website,
} from "@/lib/types";
import { Status } from "@/components/ui/status";
import { RunOutcome } from "@/components/monitors/run-outcome";
export function WebsiteDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const site = useQuery({
    queryKey: ["website", id],
    queryFn: () => api<Website>(`websites/${id}`),
  });
  const monitors = useQuery({
    queryKey: ["monitors", id],
    queryFn: () => api<Monitor[]>(`monitors?website=${id}`),
  });
  const incidents = useQuery({
    queryKey: ["incidents", { monitor: "website", id }],
    queryFn: () => api<Page<Incident>>("incidents?page_size=20"),
  });
  const run = useMutation({
    mutationFn: (monitorId: number) =>
      api<CheckResult>(`monitors/${monitorId}/run`, { method: "POST" }),
    onSuccess: async (_, monitorId) =>
      Promise.all([
        qc.invalidateQueries({
          queryKey: ["monitor-results", String(monitorId)],
        }),
        qc.invalidateQueries({ queryKey: ["monitors", id] }),
        qc.invalidateQueries({ queryKey: ["dashboard"] }),
      ]),
  });
  if (site.isLoading || monitors.isLoading)
    return <div className="card p-8 muted">Loading website…</div>;
  if (!site.data) return <div className="card p-8">Website not found.</div>;
  const current = monitors.data ?? [];
  // Website.is_active is currently metadata; monitor.is_enabled controls scheduling.
  const state =
    current.filter((m) => m.is_enabled).length === 0
      ? "PAUSED"
      : current.some((m) => m.has_open_incident)
        ? "PROBLEM"
        : current.every((m) => m.last_check_at)
          ? "HEALTHY"
          : "UNKNOWN";
  const ownIncidents = (incidents.data?.results ?? []).filter(
    (i) => i.website === site.data!.id,
  );
  return (
    <div className="stack gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="eyebrow">Website</p>
          <div className="mt-1 flex items-center gap-3">
            <h1 className="page-title">{site.data.name}</h1>
            <Status value={state} />
          </div>
          <a
            href={site.data.url}
            target="_blank"
            rel="noreferrer"
            className="muted mt-2 block hover:underline"
          >
            {site.data.url}
          </a>
        </div>
        <div className="flex gap-2">
          <Link className="button secondary" href={`/websites/${id}/edit`}>
            Edit website
          </Link>
          <Link className="button" href={`/websites/${id}/monitors/new`}>
            Add monitor
          </Link>
        </div>
      </header>
      <section className="card">
        <div className="p-5 border-b border-[#edf0ee]">
          <h2 className="font-semibold">Monitors</h2>
        </div>
        {current.length ? (
          <div className="table-wrap">
            <table className="desktop-table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Status</th>
                  <th>Interval</th>
                  <th>Last check</th>
                  <th>Next check</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {current.map((m) => (
                  <tr key={m.id}>
                    <td className="font-semibold">
                      {m.monitor_type === "FLOW"
                        ? "Contact form"
                        : m.monitor_type}
                    </td>
                    <td>
                      <Status
                        value={
                          !m.is_enabled
                            ? "DISABLED"
                            : m.has_open_incident
                              ? "PROBLEM"
                              : m.last_check_at
                                ? "HEALTHY"
                                : "UNKNOWN"
                        }
                      />
                    </td>
                    <td>{Math.round(m.interval_seconds / 60)} min</td>
                    <td>{relativeTime(m.last_check_at)}</td>
                    <td>
                      {m.is_enabled
                        ? relativeTime(m.next_check_at)
                        : "Disabled"}
                    </td>
                    <td>
                      <div className="flex flex-wrap gap-2">
                        <button
                          className="button secondary"
                          disabled={run.isPending && run.variables === m.id}
                          onClick={() => run.mutate(m.id)}
                        >
                          {run.isPending && run.variables === m.id
                            ? "Running…"
                            : "Run now"}
                        </button>
                        <Link
                          className="button secondary"
                          href={`/websites/${id}/monitors/${m.id}`}
                        >
                          Details
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="p-8 text-center">
            <p className="font-semibold">No monitors configured</p>
            <p className="muted text-sm mt-1">
              Add an HTTP, browser, or contact-form monitor.
            </p>
          </div>
        )}
      </section>
      {run.data && <RunOutcome result={run.data} />}
      {run.isError && (
        <section
          className="card border-l-4 border-l-amber-600 p-5"
          role="alert"
        >
          <h2 className="font-semibold">Uptora could not complete the check</h2>
          <p className="muted mt-1 text-sm">
            No target failure was recorded. Retry when Uptora is available.
          </p>
          <button
            className="button secondary mt-3"
            onClick={() => run.variables && run.mutate(run.variables)}
          >
            Try again
          </button>
        </section>
      )}
      <section className="card">
        <div className="p-5 border-b border-[#edf0ee]">
          <h2 className="font-semibold">Recent incidents</h2>
        </div>
        {ownIncidents.length ? (
          <ul className="divide-y divide-[#edf0ee]">
            {ownIncidents.slice(0, 8).map((i) => (
              <li className="p-5 flex justify-between gap-4" key={i.id}>
                <Link
                  className="font-semibold hover:underline"
                  href={`/incidents/${i.id}`}
                >
                  {i.failure_type}
                </Link>
                <span className="muted text-sm">
                  {i.status} · {relativeTime(i.started_at)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="p-6 muted">No incidents recorded for this website.</p>
        )}
      </section>
    </div>
  );
}
