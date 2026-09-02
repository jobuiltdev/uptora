"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { duration, exactTime } from "@/lib/format";
import type { Incident, Monitor } from "@/lib/types";
export function IncidentDetail({ id }: { id: number }) {
  const incident = useQuery({
    queryKey: ["incident", String(id)],
    queryFn: () => api<Incident>(`incidents/${id}`),
  });
  const monitor = useQuery({
    queryKey: ["monitor", String(incident.data?.monitor)],
    queryFn: () => api<Monitor>(`monitors/${incident.data!.monitor}`),
    enabled: !!incident.data,
  });
  if (incident.isLoading)
    return <div className="card p-8 muted">Loading incident…</div>;
  if (!incident.data)
    return <div className="card p-8">Incident not found.</div>;
  const i = incident.data;
  return (
    <div className="stack gap-6">
      <header>
        <p className="eyebrow">Incident #{i.id}</p>
        <h1 className="page-title mt-1">{i.website_name}</h1>
        <p
          className={`mt-2 font-semibold ${i.status === "OPEN" ? "text-red-800" : "text-emerald-800"}`}
        >
          {i.status === "OPEN" ? "Problem ongoing" : "Recovered"}
        </p>
      </header>
      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {[
          ["Monitor", monitor.data?.monitor_type ?? `#${i.monitor}`],
          ["Started", exactTime(i.started_at)],
          ["Resolved", exactTime(i.resolved_at)],
          ["Duration", duration(i.duration_seconds)],
        ].map(([label, value]) => (
          <article className="card p-5" key={label}>
            <p className="muted text-sm">{label}</p>
            <p className="font-semibold mt-2">{value}</p>
          </article>
        ))}
      </section>
      <section className="card p-6">
        <h2 className="font-semibold text-lg">What Uptora observed</h2>
        <dl className="mt-5 grid gap-5 md:grid-cols-2">
          <div>
            <dt className="muted text-sm">Original failure</dt>
            <dd className="mt-1">
              {i.initial_error_message ?? i.failure_type}
            </dd>
          </div>
          <div>
            <dt className="muted text-sm">Latest failure</dt>
            <dd className="mt-1">{i.latest_error_message ?? i.failure_type}</dd>
          </div>
          <div>
            <dt className="muted text-sm">Failed checks</dt>
            <dd className="mt-1">{i.failure_count}</dd>
          </div>
          <div>
            <dt className="muted text-sm">Recovery confirmations</dt>
            <dd className="mt-1">{i.recovery_count}</dd>
          </div>
        </dl>
        <p className="muted text-sm mt-6">
          This is an observation, not a causal diagnosis.
        </p>
      </section>
      <div>
        <Link
          className="button secondary"
          href={`/websites/${i.website}/monitors/${i.monitor}`}
        >
          View monitor and check timeline
        </Link>
      </div>
    </div>
  );
}
