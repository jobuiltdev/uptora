"use client";

import {
  Activity,
  Bell,
  Globe2,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  Settings,
  Siren,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { User } from "@/lib/types";
const links = [
  ["/dashboard", "Overview", LayoutDashboard],
  ["/websites", "Websites", Globe2],
  ["/incidents", "Incidents", Siren],
  ["/settings/notifications", "Notifications", Bell],
  ["/settings/account", "Account", Settings],
] as const;
export function AppShell({ children }: { children: React.ReactNode }) {
  const path = usePathname(),
    router = useRouter(),
    [open, setOpen] = useState(false);
  const feedbackEmail = process.env.NEXT_PUBLIC_FEEDBACK_EMAIL;
  const session = useQuery<User>({
    queryKey: ["session"],
    queryFn: async () => {
      const r = await fetch("/api/auth/session");
      if (!r.ok) {
        router.replace("/login");
        throw new Error("Session expired");
      }
      return r.json();
    },
    retry: false,
  });
  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
    router.refresh();
  }
  const nav = (
    <>
      <div className="flex items-center gap-2 px-3 py-4 text-lg font-bold">
        <Activity className="text-[#65a88d]" /> Uptora
      </div>
      <nav className="mt-5 grid gap-1" aria-label="Primary">
        {links.map(([href, label, Icon]) => (
          <Link
            key={href}
            onClick={() => setOpen(false)}
            href={href}
            className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium ${path.startsWith(href) ? "bg-white/12 text-white" : "text-[#b9cec4] hover:bg-white/7 hover:text-white"}`}
          >
            <Icon size={18} />
            {label}
          </Link>
        ))}
      </nav>
      <div className="mt-auto border-t border-white/10 pt-4">
        {feedbackEmail && (
          <a
            className="mb-2 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-[#d4e2dc] hover:bg-white/7"
            href={`mailto:${feedbackEmail}?subject=Uptora private alpha feedback`}
          >
            <MessageSquare size={17} />
            Send feedback
          </a>
        )}
        <p className="truncate px-3 text-xs text-[#a8c3b6]">
          {session.data?.email ?? "Loading account…"}
        </p>
        <button
          onClick={logout}
          className="mt-2 flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-[#d4e2dc] hover:bg-white/7"
        >
          <LogOut size={17} />
          Sign out
        </button>
      </div>
    </>
  );
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[236px_1fr]">
      <aside className="hidden lg:flex bg-[#173e2f] p-3 text-white min-h-screen sticky top-0 h-screen flex-col">
        {nav}
      </aside>
      {open && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            aria-label="Close navigation"
            className="absolute inset-0 bg-black/35"
            onClick={() => setOpen(false)}
          />
          <aside className="relative flex h-full w-72 flex-col bg-[#173e2f] p-3 text-white">
            <button
              aria-label="Close navigation"
              className="absolute right-3 top-4"
              onClick={() => setOpen(false)}
            >
              <X />
            </button>
            {nav}
          </aside>
        </div>
      )}
      <div>
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-[#dce3de] bg-white/95 px-5 backdrop-blur lg:px-8">
          <button
            aria-label="Open navigation"
            className="lg:hidden"
            onClick={() => setOpen(true)}
          >
            <Menu />
          </button>
          <p className="text-sm font-semibold">Website operations</p>
          <span className="text-xs muted">Secure session</span>
        </header>
        <main className="mx-auto max-w-[1440px] p-5 lg:p-8">{children}</main>
      </div>
    </div>
  );
}
