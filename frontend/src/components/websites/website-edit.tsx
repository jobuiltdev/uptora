"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Website } from "@/lib/types";
import { WebsiteForm } from "./website-form";

export function WebsiteEdit({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ["website", id],
    queryFn: () => api<Website>(`websites/${id}`),
  });
  if (query.isLoading)
    return <div className="card p-8 muted">Loading website…</div>;
  if (!query.data) return <div className="card p-8">Website not found.</div>;
  return <WebsiteForm website={query.data} />;
}
