"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import type { Website } from "@/lib/types";
export function WebsitesView() {
  const client = useQueryClient(),
    [remove, setRemove] = useState<Website | null>(null);
  const query = useQuery({
    queryKey: ["websites"],
    queryFn: () => api<Website[]>("websites"),
  });
  const deletion = useMutation({
    mutationFn: (id: number) => api(`websites/${id}`, { method: "DELETE" }),
    onSuccess: async () => {
      setRemove(null);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["websites"] }),
        client.invalidateQueries({ queryKey: ["dashboard"] }),
      ]);
    },
  });
  return (
    <div className="stack gap-6">
      <header className="flex items-end justify-between gap-4">
        <div>
          <p className="eyebrow">Portfolio</p>
          <h1 className="page-title">Websites</h1>
          <p className="muted mt-2">Manage the client sites Uptora watches.</p>
        </div>
        <Link className="button" href="/websites/new">
          Add website
        </Link>
      </header>
      {query.isLoading ? (
        <div className="card p-8 muted">Loading websites…</div>
      ) : query.isError ? (
        <div className="card p-8 text-red-800">
          Websites could not be loaded.
        </div>
      ) : query.data?.length ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {query.data.map((site) => (
            <article className="card p-5" key={site.id}>
              <div className="flex justify-between gap-4">
                <div>
                  <Link
                    href={`/websites/${site.id}`}
                    className="font-semibold text-lg hover:underline"
                  >
                    {site.name}
                  </Link>
                  <p className="muted text-sm mt-1 break-all">{site.url}</p>
                </div>
                <span className="text-xs font-semibold">
                  {site.is_active ? "Active record" : "Inactive record"}
                </span>
              </div>
              <div className="mt-6 flex gap-2">
                <Link
                  className="button secondary"
                  href={`/websites/${site.id}`}
                >
                  Open
                </Link>
                <button
                  className="button secondary text-red-700"
                  onClick={() => setRemove(site)}
                >
                  Delete
                </button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <div className="card p-10 text-center">
          <p className="font-semibold">No websites yet</p>
          <p className="muted mt-1">
            Add your first website to start monitoring.
          </p>
        </div>
      )}
      {remove && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4"
          role="presentation"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-title"
            className="card max-w-md p-6"
          >
            <h2 id="delete-title" className="font-semibold text-lg">
              Delete {remove.name}?
            </h2>
            <p className="muted mt-2 text-sm">
              This also removes its monitors, results, and incidents.
              Notification audit history remains protected by the backend.
            </p>
            <div className="mt-6 flex justify-end gap-2">
              <button
                className="button secondary"
                onClick={() => setRemove(null)}
              >
                Cancel
              </button>
              <button
                className="button danger"
                disabled={deletion.isPending}
                onClick={() => deletion.mutate(remove.id)}
              >
                {deletion.isPending ? "Deleting…" : "Delete website"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
