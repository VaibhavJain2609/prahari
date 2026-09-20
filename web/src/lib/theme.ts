"use client";

import { useSyncExternalStore } from "react";

// Ops consoles live in dark rooms and on wall displays, so the default is
// dark-first: an explicit stored choice wins, then prefers-color-scheme,
// then dark. The class is applied to <html> by an inline script in
// layout.tsx before first paint so there is no light flash on load.

export type Theme = "light" | "dark";

const STORAGE_KEY = "prahari-theme";

const listeners = new Set<() => void>();

export function getStoredTheme(): Theme {
  if (typeof window === "undefined") return "dark";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark");
  document.documentElement.style.colorScheme = theme;
}

export function setTheme(theme: Theme) {
  window.localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const theme = useSyncExternalStore(subscribe, getStoredTheme, () => "dark" as Theme);
  return { theme, toggle: () => setTheme(theme === "dark" ? "light" : "dark") };
}
