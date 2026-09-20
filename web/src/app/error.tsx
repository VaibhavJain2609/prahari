"use client";

import { useEffect } from "react";

// Route-level boundary for the console page: a render crash shows a
// recoverable error instead of a blank window.
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("console render error:", error);
  }, [error]);

  return (
    <div className="flex flex-1 items-center justify-center bg-slate-50 dark:bg-slate-950">
      <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <h1 className="mb-1 text-lg font-semibold text-slate-900 dark:text-slate-50">
          Console error
        </h1>
        <p className="mb-4 text-xs text-slate-500 dark:text-slate-400">
          {error.message || "Something went wrong rendering this page."}
        </p>
        <button
          onClick={reset}
          className="rounded bg-slate-900 px-3 py-2 text-sm font-medium text-white focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
        >
          Try again
        </button>
      </div>
    </div>
  );
}
