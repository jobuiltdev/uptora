"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { api, ApiError } from "@/lib/api";
import type { Website } from "@/lib/types";
type Values = { name: string; url: string; is_active: boolean };
export function WebsiteForm({ website }: { website?: Website }) {
  const router = useRouter(),
    client = useQueryClient();
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<Values>({
    defaultValues: {
      name: website?.name ?? "",
      url: website?.url ?? "",
      is_active: website?.is_active ?? true,
    },
  });
  const mutation = useMutation({
    mutationFn: (values: Values) =>
      api<Website>(website ? `websites/${website.id}` : "websites", {
        method: website ? "PATCH" : "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: async (saved) => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["websites"] }),
        client.invalidateQueries({ queryKey: ["dashboard"] }),
      ]);
      router.push(`/websites/${saved.id}`);
    },
    onError: (error) => {
      if (
        error instanceof ApiError &&
        error.data &&
        typeof error.data === "object"
      )
        for (const [field, messages] of Object.entries(error.data))
          if (field === "name" || field === "url")
            setError(field, {
              message: Array.isArray(messages)
                ? String(messages[0])
                : String(messages),
            });
    },
  });
  return (
    <form
      className="card stack max-w-2xl p-6"
      onSubmit={handleSubmit((v) => mutation.mutate(v))}
    >
      <label className="label">
        Website name
        <input
          className="input"
          placeholder="Client marketing site"
          {...register("name", { required: "Name is required." })}
        />
        <span className="text-xs text-red-700">{errors.name?.message}</span>
      </label>
      <label className="label">
        Website URL
        <input
          className="input"
          placeholder="example.com"
          {...register("url", { required: "URL is required." })}
        />
        <span className="muted text-xs">
          Uptora’s backend validates and normalizes this address.
        </span>
        <span className="text-xs text-red-700">{errors.url?.message}</span>
      </label>
      {website && (
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" {...register("is_active")} />
          Website is active
        </label>
      )}
      {mutation.isError && (
        <p role="alert" className="text-sm text-red-700">
          Review the fields and try again.
        </p>
      )}
      <div>
        <button className="button" disabled={mutation.isPending}>
          {mutation.isPending
            ? "Saving…"
            : website
              ? "Save changes"
              : "Add website"}
        </button>
      </div>
    </form>
  );
}
