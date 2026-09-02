"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Image from "next/image";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { exactTime, relativeTime } from "@/lib/format";
import type { CheckResult, Monitor, Page } from "@/lib/types";
import { Status } from "@/components/ui/status";
import { MonitorForm } from "./monitor-form";
import { RunOutcome } from "./run-outcome";
export function MonitorDetail({
  websiteId,
  monitorId,
}: {
  websiteId: number;
  monitorId: number;
}) {
  const qc = useQueryClient(),
    router = useRouter(),
    [page, setPage] = useState(1),
    [editing, setEditing] = useState(false),
    [confirmDelete, setConfirmDelete] = useState(false),
    [evidence, setEvidence] = useState<string | null>(null);
  useEffect(
    () => () => {
      if (evidence) URL.revokeObjectURL(evidence);
    },
    [evidence],
  );
  const monitor = useQuery({
    queryKey: ["monitor", String(monitorId)],
    queryFn: () => api<Monitor>(`monitors/${monitorId}`),
  });
  const results = useQuery({
    queryKey: ["monitor-results", String(monitorId), page],
    queryFn: () =>
      api<Page<CheckResult>>(
        `monitors/${monitorId}/results?page=${page}&page_size=20`,
      ),
  });
  const run = useMutation({
    mutationFn: () =>
      api<CheckResult>(`monitors/${monitorId}/run`, { method: "POST" }),
    onSuccess: () =>
      Promise.all([
        qc.invalidateQueries({
          queryKey: ["monitor-results", String(monitorId)],
        }),
        qc.invalidateQueries({ queryKey: ["monitor", String(monitorId)] }),
        qc.invalidateQueries({ queryKey: ["dashboard"] }),
      ]),
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) =>
      api<Monitor>(`monitors/${monitorId}`, {
        method: "PATCH",
        body: JSON.stringify({ is_enabled: enabled }),
      }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["monitor", String(monitorId)] }),
  });
  const deletion = useMutation({
    mutationFn: () => api(`monitors/${monitorId}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["monitors", String(websiteId)] });
      router.push(`/websites/${websiteId}`);
    },
  });
  async function showEvidence(id: number) {
    const response = await fetch(`/api/bff/check-results/${id}/evidence`);
    if (response.ok) setEvidence(URL.createObjectURL(await response.blob()));
  }
  if (monitor.isLoading)
    return <div className="card p-8 muted">Loading monitor…</div>;
  if (!monitor.data) return <div className="card p-8">Monitor not found.</div>;
  const m = monitor.data;
  if (editing)
    return (
      <div className="stack">
        <button
          className="button secondary justify-self-start"
          onClick={() => setEditing(false)}
        >
          Back to details
        </button>
        <MonitorForm websiteId={websiteId} monitor={m} />
      </div>
    );
  return (
    <div className="stack gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="eyebrow">
            {m.monitor_type === "FLOW" ? "Contact form" : m.monitor_type}{" "}
            monitor
          </p>
          <div className="flex items-center gap-3 mt-1">
            <h1 className="page-title">Monitor #{m.id}</h1>
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
          </div>
          <p className="muted mt-2">
            Last checked {relativeTime(m.last_check_at)} · next{" "}
            {m.is_enabled ? relativeTime(m.next_check_at) : "disabled"}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className="button"
            disabled={run.isPending || !m.is_enabled}
            onClick={() => run.mutate()}
          >
            {run.isPending ? "Running…" : "Run now"}
          </button>
          <button className="button secondary" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button
            className="button secondary"
            disabled={toggle.isPending}
            onClick={() => toggle.mutate(!m.is_enabled)}
          >
            {m.is_enabled ? "Disable" : "Enable"}
          </button>
          <button
            className="button secondary text-red-700"
            onClick={() => setConfirmDelete(true)}
          >
            Delete
          </button>
        </div>
      </header>
      {run.data && <RunOutcome result={run.data} />}
      <section className="grid gap-4 sm:grid-cols-3">
        <article className="card p-5">
          <p className="muted text-sm">Interval</p>
          <p className="mt-2 font-semibold">{m.interval_seconds}s</p>
        </article>
        <article className="card p-5">
          <p className="muted text-sm">Timeout</p>
          <p className="mt-2 font-semibold">{m.timeout_seconds}s</p>
        </article>
        <article className="card p-5">
          <p className="muted text-sm">Schedule</p>
          <p className="mt-2 font-semibold">
            {m.is_enabled ? "Enabled" : "Disabled"}
          </p>
        </article>
      </section>
      <section className="card">
        <div className="p-5 border-b border-[#edf0ee]">
          <h2 className="font-semibold">Check history</h2>
        </div>
        {results.isLoading ? (
          <p className="p-6 muted">Loading results…</p>
        ) : results.data?.results.length ? (
          <>
            <div className="table-wrap">
              <table className="desktop-table">
                <thead>
                  <tr>
                    <th>Result</th>
                    <th>Checked</th>
                    <th>Status</th>
                    <th>Response</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {results.data.results.map((r) => (
                    <tr key={r.id}>
                      <td
                        className={
                          r.is_success
                            ? "text-emerald-800 font-semibold"
                            : "text-red-800 font-semibold"
                        }
                      >
                        {r.is_success ? "Passed" : "Failed"}
                      </td>
                      <td title={exactTime(r.checked_at)}>
                        {relativeTime(r.checked_at)}
                      </td>
                      <td>{r.status_code ?? "—"}</td>
                      <td>
                        {r.response_time_ms === null
                          ? "—"
                          : `${r.response_time_ms} ms`}
                      </td>
                      <td>
                        <p className="max-w-xl text-sm">
                          {r.error_message ??
                            r.final_url ??
                            "No additional detail."}
                        </p>
                        {r.ssl_expires_at && (
                          <p className="muted text-xs mt-1">
                            TLS expires {exactTime(r.ssl_expires_at)}
                          </p>
                        )}
                        {r.screenshot && (
                          <button
                            className="mt-2 text-sm font-semibold text-[#1f6b4f] hover:underline"
                            onClick={() => showEvidence(r.id)}
                          >
                            View screenshot
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex justify-between p-4">
              <button
                className="button secondary"
                disabled={!results.data.previous}
                onClick={() => setPage((p) => p - 1)}
              >
                Previous
              </button>
              <span className="muted text-sm self-center">Page {page}</span>
              <button
                className="button secondary"
                disabled={!results.data.next}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          </>
        ) : (
          <p className="p-8 text-center muted">No checks have run yet.</p>
        )}
      </section>
      {evidence && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Failure screenshot"
            className="max-h-[92vh] max-w-5xl overflow-auto rounded-xl bg-white p-3"
          >
            <div className="flex justify-end pb-2">
              <button
                className="button secondary"
                onClick={() => {
                  URL.revokeObjectURL(evidence);
                  setEvidence(null);
                }}
              >
                Close
              </button>
            </div>
            <Image
              unoptimized
              width={1440}
              height={900}
              src={evidence}
              alt="Screenshot captured when the check failed"
              className="h-auto max-w-full"
            />
          </div>
        </div>
      )}
      {confirmDelete && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4">
          <div role="dialog" aria-modal="true" className="card max-w-md p-6">
            <h2 className="font-semibold text-lg">Delete this monitor?</h2>
            <p className="muted text-sm mt-2">
              Its check history and incidents will also be removed.
            </p>
            <div className="mt-6 flex justify-end gap-2">
              <button
                className="button secondary"
                onClick={() => setConfirmDelete(false)}
              >
                Cancel
              </button>
              <button
                className="button danger"
                disabled={deletion.isPending}
                onClick={() => deletion.mutate()}
              >
                Delete monitor
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
