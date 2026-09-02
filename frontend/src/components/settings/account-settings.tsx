"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { User } from "@/lib/types";

export function AccountSettings() {
  const router = useRouter();
  const session = useQuery<User>({
    queryKey: ["session"],
    queryFn: async () => {
      const response = await fetch("/api/auth/session");
      if (!response.ok) throw new Error("session unavailable");
      return response.json();
    },
    retry: false,
  });
  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
    router.refresh();
  }
  return (
    <div className="stack max-w-3xl gap-6">
      <header>
        <p className="eyebrow">Settings</p>
        <h1 className="page-title mt-1">Account</h1>
        <p className="muted mt-2">
          Your private-alpha identity and account actions.
        </p>
      </header>
      <section className="card p-6">
        <h2 className="font-semibold">Account email</h2>
        <p className="mt-2">{session.data?.email ?? "Loading account…"}</p>
      </section>
      <section className="card p-6">
        <h2 className="font-semibold">Notifications</h2>
        <p className="muted mt-2 text-sm">
          Choose the address that receives confirmed outage and recovery alerts.
        </p>
        <Link className="button secondary mt-4" href="/settings/notifications">
          Notification settings
        </Link>
      </section>
      <section className="card p-6">
        <h2 className="font-semibold">Password support</h2>
        <p className="muted mt-2 text-sm">
          Self-service password reset is not available during private alpha.
          Contact the Uptora operator for help.
        </p>
      </section>
      <div>
        <button className="button danger" onClick={logout}>
          Sign out
        </button>
      </div>
    </div>
  );
}
