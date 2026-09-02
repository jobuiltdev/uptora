"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, CircleCheck, Globe2, Siren } from "lucide-react";
import Link from "next/link";
import { api } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import type { Dashboard } from "@/lib/types";
import { Status } from "@/components/ui/status";
export function DashboardView() {
  const query = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("dashboard"),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });
  if (query.isLoading) return <State text="Loading portfolio health…" />;
  if (query.isError)
    return (
      <State
        text="The dashboard could not reach Uptora. Your monitors continue running in the background."
        retry={() => query.refetch()}
      />
    );
  const data = query.data!;
  const cards = [
    ["Websites", data.summary.websites, Globe2],
    ["Healthy monitors", data.summary.healthy_monitors, CircleCheck],
    ["Active incidents", data.summary.active_incidents, Siren],
    ["Checks in 24h", data.summary.checks_24h, Activity],
  ] as const;
  return (
    <div className="stack gap-6">
      <header>
        <p className="eyebrow">Portfolio health</p>
        <h1 className="page-title mt-1">Operational overview</h1>
        <p className="muted mt-2">
          Confirmed incidents, recent checks, and what needs attention now.
        </p>
      </header>
      <OnboardingChecklist data={data} />
      <section className="grid-cards">
        {cards.map(([label, value, Icon]) => (
          <article className="card p-5" key={label}>
            <div className="flex items-center justify-between">
              <p className="muted text-sm">{label}</p>
              <Icon size={18} className="text-[#1f6b4f]" />
            </div>
            <p className="mt-3 text-3xl font-semibold">{value}</p>
          </article>
        ))}
      </section>
      <section className="card">
        <div className="flex items-center justify-between p-5 border-b border-[#edf0ee]">
          <div>
            <h2 className="font-semibold text-lg">Website portfolio</h2>
            <p className="muted text-sm">Health across enabled monitors.</p>
          </div>
          <Link className="button" href="/websites/new">
            Add website
          </Link>
        </div>
        {data.websites.length ? (
          <div className="table-wrap">
            <table className="desktop-table">
              <thead>
                <tr>
                  <th>Website</th>
                  <th>Status</th>
                  <th>Monitors</th>
                  <th>Incidents</th>
                  <th>Last check</th>
                </tr>
              </thead>
              <tbody>
                {data.websites.map((site) => (
                  <tr key={site.id}>
                    <td>
                      <Link
                        className="font-semibold hover:underline"
                        href={`/websites/${site.id}`}
                      >
                        {site.name}
                      </Link>
                      <div className="muted text-xs mt-1 truncate max-w-sm">
                        {site.url}
                      </div>
                    </td>
                    <td>
                      <Status value={site.health} />
                    </td>
                    <td>
                      {site.enabled_monitor_count}/{site.monitor_count} enabled
                      <div className="mt-1 flex gap-1">
                        {site.monitor_types.map((t) => (
                          <span
                            key={t}
                            className="text-[11px] rounded bg-[#edf3ef] px-1.5 py-0.5"
                          >
                            {t === "FLOW" ? "FORM" : t}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td>{site.active_incident_count || "Clear"}</td>
                    <td title={site.last_checked_at ?? undefined}>
                      {relativeTime(site.last_checked_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title="No websites yet"
            text="Add your first website to start monitoring what matters beyond uptime."
          />
        )}
      </section>
      <div className="grid gap-6 xl:grid-cols-2">
        <section className="card">
          <div className="p-5 border-b border-[#edf0ee]">
            <h2 className="font-semibold">Active incidents</h2>
          </div>
          {data.active_incidents.length ? (
            <ul className="divide-y divide-[#edf0ee]">
              {data.active_incidents.map((i) => (
                <li className="p-5" key={i.id}>
                  <div className="flex justify-between gap-3">
                    <div>
                      <Link
                        className="font-semibold hover:underline"
                        href={`/incidents/${i.id}`}
                      >
                        {i.website_name}
                      </Link>
                      <p className="muted text-sm mt-1">
                        {i.monitor_type} ·{" "}
                        {i.latest_error_message || i.failure_type}
                      </p>
                    </div>
                    <span className="text-xs text-red-700">
                      {relativeTime(i.started_at)}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty
              title="No active incidents"
              text="All monitored systems are currently clear."
            />
          )}
        </section>
        <section className="card">
          <div className="p-5 border-b border-[#edf0ee]">
            <h2 className="font-semibold">Recent checks</h2>
          </div>
          {data.recent_checks.length ? (
            <ul className="divide-y divide-[#edf0ee]">
              {data.recent_checks.slice(0, 8).map((r) => (
                <li className="p-4 flex justify-between gap-3" key={r.id}>
                  <div>
                    <span
                      className={`font-semibold ${r.is_success ? "text-emerald-800" : "text-red-800"}`}
                    >
                      {r.is_success ? "Passed" : "Failed"}
                    </span>
                    <span className="muted text-sm">
                      {" "}
                      · {r.website_name} · {r.monitor_type}
                    </span>
                  </div>
                  <time
                    title={new Date(r.checked_at).toLocaleString()}
                    className="muted text-xs"
                  >
                    {relativeTime(r.checked_at)}
                  </time>
                </li>
              ))}
            </ul>
          ) : (
            <Empty
              title="No checks yet"
              text="Results appear here after a monitor runs."
            />
          )}
        </section>
      </div>
    </div>
  );
}
export function OnboardingChecklist({ data }: { data: Dashboard }) {
  const steps = [
    {
      done: data.summary.websites > 0,
      title: "Add your first website",
      text: "Choose the client website Uptora should watch.",
      href: "/websites/new",
      action: "Add website",
    },
    {
      done: data.summary.enabled_monitors > 0,
      title: "Create a monitor",
      text: "HTTP checks availability; Browser checks rendered content; Contact form submits a real test form.",
      href: data.websites[0]
        ? `/websites/${data.websites[0].id}`
        : "/websites/new",
      action: "Choose monitor",
    },
    {
      done: data.summary.checks_24h > 0,
      title: "Run and understand a check",
      text: "A failed check describes the target. An internal Uptora error is shown separately and never opens an incident.",
      href: data.websites[0]
        ? `/websites/${data.websites[0].id}`
        : "/websites/new",
      action: "View website",
    },
  ];
  if (steps.every((step) => step.done)) return null;
  return (
    <section className="card p-6" aria-labelledby="getting-started-title">
      <p className="eyebrow">Private alpha</p>
      <h2 id="getting-started-title" className="mt-1 text-lg font-semibold">
        Getting started
      </h2>
      <div className="mt-5 grid gap-4 lg:grid-cols-3">
        {steps.map((step, index) => (
          <article
            className="rounded-xl border border-[#dce3de] p-4"
            key={step.title}
          >
            <p className="text-sm font-semibold">
              {step.done ? "✓" : index + 1} · {step.title}
            </p>
            <p className="muted mt-2 text-sm">{step.text}</p>
            {!step.done && (
              <Link
                className="mt-3 inline-block text-sm font-semibold text-[#1f6b4f]"
                href={step.href}
              >
                {step.action} →
              </Link>
            )}
          </article>
        ))}
      </div>
      <p className="muted mt-5 text-sm">
        Two consecutive failures open an incident. Configure alerts under
        Notifications, then share a read-only client report from an incident.
      </p>
    </section>
  );
}

function State({ text, retry }: { text: string; retry?: () => void }) {
  return (
    <div className="card p-8" role="status">
      <p className="muted">{text}</p>
      {retry && (
        <button className="button secondary mt-4" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}
function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div className="p-8 text-center">
      <p className="font-semibold">{title}</p>
      <p className="muted text-sm mt-1">{text}</p>
    </div>
  );
}
