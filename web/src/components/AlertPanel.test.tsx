import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import AlertPanel from "./AlertPanel";

// The panel subscribes to the shared stream in lib/alerts.ts, whose
// EventSource is a module-level singleton — so the mock captures it once
// and both tests drive the same instance, exactly like the real console.
class MockEventSource {
  static instances: MockEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  private listeners = new Map<string, ((event: MessageEvent) => void)[]>();

  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, cb: (event: MessageEvent) => void) {
    const list = this.listeners.get(type) ?? [];
    list.push(cb);
    this.listeners.set(type, list);
  }

  emit(type: string, data: unknown) {
    for (const cb of this.listeners.get(type) ?? []) {
      cb({ data: JSON.stringify(data) } as MessageEvent);
    }
  }

  close() {
    this.readyState = 2;
  }
}

const ALERT = {
  alert_id: "alert-1",
  raised_at: new Date(Date.now() - 90_000).toISOString(),
  priority: "ALERT_PRIORITY_CRITICAL",
  detection: {
    camera_id: "cam-7",
    plate: { raw_text: "GJ01AB 1234", normalised_text: "GJ01AB1234" },
  },
  matched_entry: {
    plate: "GJ01AB1234",
    reason: "WATCHLIST_REASON_STOLEN",
    case_reference: "FIR-9",
  },
  explanation: {
    observed_plate: "GJ01AB1234",
    matched_plate: "GJ01AB1234",
    edits: [],
    final_score: 0.98,
  },
};

beforeAll(() => {
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(cleanup);

function stream(): MockEventSource {
  const es = MockEventSource.instances[0];
  expect(es.url).toBe("/api/bff/alerts/stream");
  act(() => es.onopen?.());
  return es;
}

describe("AlertPanel", () => {
  it("renders an alert with priority, reason and a raised-at time", () => {
    render(<AlertPanel />);
    const es = stream();
    act(() => es.emit("alert", ALERT));

    expect(screen.getByText("GJ01AB1234 at cam-7")).toBeTruthy();
    expect(screen.getByText("CRITICAL")).toBeTruthy();
    expect(screen.getByText(/stolen/)).toBeTruthy();
    expect(screen.getByText(/m ago/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /match explanation/i })).toBeTruthy();
  });

  it("dedupes a replayed alert_id instead of adding a second row", () => {
    render(<AlertPanel />);
    const es = stream();
    act(() => es.emit("alert", ALERT));
    act(() => es.emit("alert", ALERT));

    expect(screen.getAllByText("GJ01AB1234 at cam-7")).toHaveLength(1);
  });
});
