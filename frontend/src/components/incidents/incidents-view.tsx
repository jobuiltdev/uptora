"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { duration, relativeTime } from "@/lib/format";
import type { Incident, Page } from "@/lib/types";
export function IncidentsView() {
  const [filter, setFilter] = useState<"OPEN" | "RESOLVED" | "ALL">("OPEN");
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ["incidents", { filter, page }],
    queryFn: () =>
      api<Page<Incident>>(
        `incidents?page=${page}&page_size=50${filter === "ALL" ? "" : `&status=${filter}`}`,
      ),
  });
  return (
    <div className="stack gap-6">
      <header>
        <p className="eyebrow">Confirmed events</p>
        <h1 className="page-title">Incidents</h1>
        <p className="muted mt-2">
          Uptora opens incidents after two consecutive failures.
        </p>
      </header>
      <div className="flex gap-2" role="group" aria-label="Incident status">
        {(["OPEN", "RESOLVED", "ALL"] as const).map((item) => (
          <button
            key={item}
            className={`button ${filter === item ? "" : "secondary"}`}
            onClick={() => {
              setFilter(item);
              setPage(1);
            }}
          >
            {item === "OPEN"
              ? "Open"
              : item === "RESOLVED"
                ? "Resolved"
                : "All"}
          </button>
        ))}
      </div>
      <section className="card">
        {query.isLoading ? (
          <p className="p-8 muted">Loading incidents…</p>
        ) : query.isError ? (
          <p className="p-8 text-red-800">Incidents could not be loaded.</p>
        ) : query.data?.results.length ? (
          <>
            <div className="table-wrap">
              <table className="desktop-table">
                <thead>
                  <tr>
                    <th>Website</th>
                    <th>State</th>
                    <th>Observed</th>
                    <th>Duration</th>
                    <th>Failure</th>
                  </tr>
                </thead>
                <tbody>
                  {query.data.results.map((i) => (
                    <tr key={i.id}>
                      <td>
                        <Link
                          href={`/incidents/${i.id}`}
                          className="font-semibold hover:underline"
                        >
                          {i.website_name}
                        </Link>
                        <p className="muted text-xs">Monitor #{i.monitor}</p>
                      </td>
                      <td>
                        <span
                          className={`text-xs font-semibold ${i.status === "OPEN" ? "text-red-800" : "text-emerald-800"}`}
                        >
                          {i.status}
                        </span>
                      </td>
                      <td>{relativeTime(i.started_at)}</td>
                      <td>{duration(i.duration_seconds)}</td>
                      <td>
                        <p className="font-medium">{i.failure_type}</p>
                        <p className="muted text-sm max-w-lg">
                          {i.latest_error_message ??
                            "No failure detail recorded."}
                        </p>
                        <p className="muted text-xs mt-1">
                          {i.failure_count} failed checks
                        </p>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex justify-between p-4">
              <button
                className="button secondary"
                disabled={!query.data.previous}
                onClick={() => setPage((p) => p - 1)}
              >
                Previous
              </button>
              <span className="muted text-sm self-center">Page {page}</span>
              <button
                className="button secondary"
                disabled={!query.data.next}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          </>
        ) : (
          <div className="p-10 text-center">
            <p className="font-semibold">
              {filter === "OPEN" ? "No active incidents" : "No incidents found"}
            </p>
            <p className="muted text-sm mt-1">
              {filter === "OPEN"
                ? "All monitored systems are currently clear."
                : "Incidents will appear after confirmed failures."}
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
