"use client";

import { useTheme } from "@/lib/theme";

// Light/dark switch for the header. The theme itself lives in lib/theme.ts
// (localStorage + a `dark` class on <html>); this is just the button.
export default function ThemeToggle() {
  const { theme, toggle } = useTheme();
  return (
    <button
      onClick={toggle}
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
    >
      {theme === "dark" ? "Light" : "Dark"}
    </button>
  );
}
