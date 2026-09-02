"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { NotificationPreference } from "@/lib/types";
type Values = { email_enabled: boolean; alert_email: string };
export function NotificationSettings() {
  const qc = useQueryClient(),
    [saved, setSaved] = useState(false);
  const query = useQuery({
    queryKey: ["notification-preferences"],
    queryFn: () => api<NotificationPreference>("notifications/preferences"),
  });
  const form = useForm<Values>();
  useEffect(() => {
    if (query.data)
      form.reset({
        email_enabled: query.data.email_enabled,
        alert_email: query.data.alert_email ?? "",
      });
  }, [query.data, form]);
  const mutation = useMutation({
    mutationFn: (v: Values) =>
      api<NotificationPreference>("notifications/preferences", {
        method: "PATCH",
        body: JSON.stringify({
          email_enabled: v.email_enabled,
          alert_email: v.alert_email || null,
        }),
      }),
    onSuccess: (data) => {
      qc.setQueryData(["notification-preferences"], data);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    },
  });
  if (query.isLoading)
    return <div className="card p-8 muted">Loading notification settings…</div>;
  return (
    <form
      className="card stack max-w-2xl p-6"
      onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
    >
      <label className="flex items-start gap-3">
        <input
          className="mt-1"
          type="checkbox"
          {...form.register("email_enabled")}
        />
        <span>
          <strong className="block">Email alerts enabled</strong>
          <span className="muted text-sm">
            Receive one alert for a confirmed outage and one when it recovers.
          </span>
        </span>
      </label>
      <label className="label">
        Alert email
        <input
          className="input"
          type="email"
          placeholder="Uses your account email when blank"
          {...form.register("alert_email")}
        />
        <span className="muted text-xs">
          Leave blank to use your Uptora account email.
        </span>
      </label>
      {mutation.isError && (
        <p role="alert" className="text-sm text-red-700">
          {mutation.error instanceof ApiError
            ? mutation.error.message
            : "Settings could not be saved."}
        </p>
      )}
      {saved && (
        <p role="status" className="text-sm text-emerald-800">
          Notification settings saved.
        </p>
      )}
      <div>
        <button className="button" disabled={mutation.isPending}>
          {mutation.isPending ? "Saving…" : "Save settings"}
        </button>
      </div>
    </form>
  );
}
