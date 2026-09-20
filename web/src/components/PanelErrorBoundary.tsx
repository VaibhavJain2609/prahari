"use client";

import { Component, ReactNode } from "react";

// Per-panel boundary: one sidebar panel throwing on bad data must not blank
// the whole console. The panel degrades to its title plus a retry affordance
// and the rest of the rail keeps working.
export default class PanelErrorBoundary extends Component<
  { title: string; children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error(`panel "${this.props.title}" crashed:`, error);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="text-xs">
          <p className="mb-1 text-red-600 dark:text-red-400">
            {this.state.error.message || "This panel crashed."}
          </p>
          <button
            onClick={() => this.setState({ error: null })}
            className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-700 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            Retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
