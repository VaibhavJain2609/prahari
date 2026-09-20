"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";

export default function LoginPage() {
  // useSearchParams() suspends during prerender — the form has to sit under
  // a Suspense boundary or `next build` fails the whole page.
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.login(username, password);
      router.replace(searchParams.get("next") || "/");
      router.refresh();
    } catch (err) {
      // A 401 is the service saying the credentials were rejected. Anything
      // else — 5xx, the proxy's 502, a network failure — means the console
      // never got a verdict at all, and telling the operator "invalid
      // credentials" would send them retyping a correct password.
      setError(
        err instanceof ApiError && err.status === 401
          ? "Invalid username or password."
          : "Could not reach the console backend. Check connectivity and try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-1 items-center justify-center bg-slate-50 dark:bg-slate-950">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
      >
        <h1 className="mb-1 text-lg font-semibold text-slate-900 dark:text-slate-50">
          PRAHARI
        </h1>
        <p className="mb-6 text-xs text-slate-500 dark:text-slate-400">
          Gujarat Sentinel — sign in to your org&apos;s console.
        </p>

        <label
          htmlFor="login-username"
          className="mb-1 block text-xs font-medium text-slate-700 dark:text-slate-300"
        >
          Username
        </label>
        <input
          id="login-username"
          autoFocus
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          className="mb-4 w-full rounded border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
        />

        <label
          htmlFor="login-password"
          className="mb-1 block text-xs font-medium text-slate-700 dark:text-slate-300"
        >
          Password
        </label>
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mb-4 w-full rounded border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
        />

        {error && (
          <p role="alert" className="mb-4 text-xs text-red-600 dark:text-red-400">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting || !username || !password}
          className="w-full rounded bg-slate-900 px-3 py-2 text-sm font-medium text-white focus:outline-none focus:ring-2 focus:ring-slate-400 focus:ring-offset-2 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:focus:ring-offset-slate-900"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
