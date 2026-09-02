"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";

type Values = { email: string; password: string };
export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [registrationEnabled, setRegistrationEnabled] = useState<
    boolean | null
  >(mode === "login" ? true : null);
  const {
    register,
    handleSubmit,
    formState: { isSubmitting },
  } = useForm<Values>();
  useEffect(() => {
    if (mode === "login") return;
    fetch("/api/auth/registration-status", { cache: "no-store" })
      .then(async (response) => {
        const data = (await response.json()) as { enabled?: boolean };
        setRegistrationEnabled(response.ok && data.enabled === true);
      })
      .catch(() => setRegistrationEnabled(false));
  }, [mode]);
  async function submit(values: Values) {
    setError("");
    try {
      const response = await fetch(`/api/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        setError(
          data.detail ??
            data.email?.[0] ??
            data.password?.[0] ??
            "Please check your details and try again.",
        );
        return;
      }
      router.replace("/dashboard");
      router.refresh();
    } catch {
      setError("Uptora could not be reached. Please try again.");
    }
  }
  const login = mode === "login";
  return (
    <main className="min-h-screen grid lg:grid-cols-[1.05fr_.95fr]">
      <section className="hidden lg:flex bg-[#173e2f] text-white p-14 flex-col justify-between">
        <div className="text-xl font-bold">Uptora</div>
        <div className="max-w-xl">
          <p className="text-4xl font-semibold leading-tight">
            Your website can be online and still be broken.
          </p>
          <p className="mt-5 text-[#c9ded4] text-lg">
            Know when client sites stop working—not merely when they stop
            responding.
          </p>
        </div>
        <p className="text-sm text-[#a8c3b6]">
          Operational monitoring for agencies and freelancers.
        </p>
      </section>
      <section className="flex items-center justify-center p-6">
        <div className="w-full max-w-md">
          <p className="eyebrow">Uptora account</p>
          <h1 className="page-title mt-2">
            {login ? "Welcome back" : "Start monitoring with confidence"}
          </h1>
          <p className="muted mt-2">
            {login
              ? "Sign in to view your portfolio health."
              : "Create your account. You will be signed in securely."}
          </p>
          {!login && registrationEnabled === null ? (
            <p className="card mt-8 p-5 muted" role="status">
              Checking private-alpha availability…
            </p>
          ) : !login && !registrationEnabled ? (
            <div className="card mt-8 p-6">
              <h2 className="font-semibold">Private alpha</h2>
              <p className="muted mt-2 text-sm">
                New account registration is currently limited. Ask the Uptora
                operator to admit your account, then sign in normally.
              </p>
              <Link className="button mt-5" href="/login">
                Sign in
              </Link>
            </div>
          ) : (
            <form className="stack mt-8" onSubmit={handleSubmit(submit)}>
              <label className="label">
                Email
                <input
                  className="input"
                  type="email"
                  autoComplete="email"
                  required
                  {...register("email")}
                />
              </label>
              <label className="label">
                Password
                <input
                  className="input"
                  type="password"
                  autoComplete={login ? "current-password" : "new-password"}
                  minLength={8}
                  required
                  {...register("password")}
                />
              </label>
              {error && (
                <p role="alert" className="text-sm text-[#a43b36]">
                  {error}
                </p>
              )}
              <button className="button" disabled={isSubmitting}>
                {isSubmitting
                  ? "Please wait…"
                  : login
                    ? "Sign in"
                    : "Create account"}
              </button>
            </form>
          )}
          <p className="mt-6 text-sm muted">
            {login ? "New to Uptora?" : "Already have an account?"}{" "}
            <Link
              className="font-semibold text-[#1f6b4f]"
              href={login ? "/register" : "/login"}
            >
              {login ? "Create an account" : "Sign in"}
            </Link>
          </p>
        </div>
      </section>
    </main>
  );
}
