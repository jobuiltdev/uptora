"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { exactTime } from "@/lib/format";
import type { IncidentShare } from "@/lib/types";

function expiry(days: string) {
  if (!days) return null;
  const date = new Date();
  date.setUTCDate(date.getUTCDate() + Number(days));
  return date.toISOString();
}

export function ShareReportSection({ incidentId }: { incidentId: number }) {
  const queryClient = useQueryClient();
  const [includeEvidence, setIncludeEvidence] = useState(true);
  const [expiryDays, setExpiryDays] = useState("");
  const [confirm, setConfirm] = useState<"revoke" | "regenerate" | null>(null);
  const [copied, setCopied] = useState(false);
  const key = ["incident-share", String(incidentId)];
  const query = useQuery({
    queryKey: key,
    queryFn: () => api<IncidentShare>(`incidents/${incidentId}/share`),
    retry: false,
  });
  const noShare = query.error instanceof ApiError && query.error.status === 404;
  const payload = () => ({
    include_evidence: includeEvidence,
    expires_at: expiry(expiryDays),
  });
  const create = useMutation({
    mutationFn: () =>
      api<IncidentShare>(`incidents/${incidentId}/share`, {
        method: "POST",
        body: JSON.stringify(payload()),
      }),
    onSuccess: (data) => queryClient.setQueryData(key, data),
  });
  const revoke = useMutation({
    mutationFn: () =>
      api<null>(`incidents/${incidentId}/share`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.setQueryData(key, null);
      setConfirm(null);
      void query.refetch();
    },
  });
  const regenerate = useMutation({
    mutationFn: () =>
      api<IncidentShare>(`incidents/${incidentId}/share/regenerate`, {
        method: "POST",
        body: JSON.stringify(payload()),
      }),
    onSuccess: (data) => {
      queryClient.setQueryData(key, data);
      setConfirm(null);
    },
  });

  const share = query.data;
  return (
    <section className="card p-6 stack" aria-labelledby="share-report-title">
      <div>
        <h2 id="share-report-title" className="font-semibold text-lg">
          Share report
        </h2>
        <p className="muted text-sm mt-1">
          Create a read-only incident proof link for your client.
        </p>
      </div>
      {query.isLoading ? (
        <p className="muted">Loading share settings…</p>
      ) : query.isError && !noShare ? (
        <p role="alert" className="text-red-800">
          Share settings could not be loaded.
        </p>
      ) : share ? (
        <>
          <label className="label">
            Client report link
            <input className="input" readOnly value={share.share_url} />
          </label>
          <p className="muted text-sm">
            {share.expires_at
              ? `Expires ${exactTime(share.expires_at)}`
              : "No expiration"}
            {` · Evidence ${share.include_evidence ? "included" : "hidden"}`}
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              className="button"
              onClick={async () => {
                await navigator.clipboard.writeText(share.share_url);
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy share link"}
            </button>
            <button
              className="button secondary"
              onClick={() => setConfirm("regenerate")}
            >
              Regenerate link
            </button>
            <button
              className="button danger"
              onClick={() => setConfirm("revoke")}
            >
              Revoke link
            </button>
          </div>
          {confirm && (
            <div className="rounded-lg border border-red-200 bg-red-50 p-4">
              <p className="font-semibold">
                {confirm === "revoke"
                  ? "This link will stop working immediately."
                  : "The current link will stop working and be replaced."}
              </p>
              <div className="flex gap-2 mt-3">
                <button
                  className="button danger"
                  onClick={() =>
                    confirm === "revoke" ? revoke.mutate() : regenerate.mutate()
                  }
                >
                  Confirm {confirm}
                </button>
                <button
                  className="button secondary"
                  onClick={() => setConfirm(null)}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </>
      ) : (
        <>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={includeEvidence}
              onChange={(event) => setIncludeEvidence(event.target.checked)}
            />
            Include screenshot evidence when available
          </label>
          <label className="label max-w-xs">
            Link expiration
            <select
              className="input"
              value={expiryDays}
              onChange={(event) => setExpiryDays(event.target.value)}
            >
              <option value="">No expiration</option>
              <option value="7">7 days</option>
              <option value="30">30 days</option>
            </select>
          </label>
          <div>
            <button
              className="button"
              disabled={create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Creating…" : "Create share link"}
            </button>
          </div>
          {create.isError && (
            <p role="alert" className="text-red-800">
              The share link could not be created.
            </p>
          )}
        </>
      )}
    </section>
  );
}
