import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import AlertsTable from "./AlertsTable";
import { AlertFilters } from "@/lib/alert-history";

// Same pattern as AlertsRail.test: the shared stream in lib/alerts.ts is a
// module-level EventSource singleton, so the mock captures it once and the
// tests drive that instance.
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

const NO_FILTERS: AlertFilters = { plate: "", camera: "", since: "", ack: "all" };

const STORED = {
  id: 41,
  alert_id: "alert-1",
  dedup_key: "cam-7|GJ01AB1234|b1",
  occurred_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  acknowledged_at: null,
  acknowledged_by: null,
  priority: "ALERT_PRIORITY_CRITICAL",
  detection: {
    camera_id: "cam-7",
    plate: { raw_text: "GJ01AB 1234", normalised_text: "GJ01AB1234" },
  },
  matched_entry: { plate: "GJ01AB1234", reason: "WATCHLIST_REASON_STOLEN" },
  explanation: { observed_plate: "GJ01AB1234", matched_plate: "GJ01AB1234", final_score: 0.98, edits: [] },
};

const LIVE = {
  alert_id: "alert-2",
  dedup_key: "cam-9|GJ05XY9999|b1",
  raised_at: new Date().toISOString(),
  priority: "ALERT_PRIORITY_HIGH",
  detection: {
    camera_id: "cam-9",
    plate: { normalised_text: "GJ05XY9999" },
    observed_at: { wall_clock: new Date().toISOString() },
  },
  matched_entry: { plate: "GJ05XY9999", reason: "WATCHLIST_REASON_WANTED" },
  explanation: { observed_plate: "GJ05XY9999", matched_plate: "GJ05XY9999", final_score: 0.91 },
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stream(): MockEventSource {
  const es = MockEventSource.instances[0];
  expect(es.url).toBe("/api/bff/alerts/stream");
  act(() => es.onopen?.());
  return es;
}

beforeAll(() => {
  vi.stubGlobal("EventSource", MockEventSource);
});

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json([STORED])));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // The module-level EventSource singleton persists across tests in this
  // file (by design — same as the real console), so the stub must go back
  // up for the next test's subscription... it never unsubscribes; re-stub
  // before each test instead.
  vi.stubGlobal("EventSource", MockEventSource);
});

describe("AlertsTable", () => {
  it("serializes filters into the BFF query and renders persisted rows", async () => {
    render(
      <AlertsTable
        filters={{ ...NO_FILTERS, camera: "cam-7", ack: "unacked" }}
        onFilters={vi.fn()}
      />,
    );

    await screen.findAllByText("GJ01AB1234");
    const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/bff/alerts?");
    expect(url).toContain("camera_id=cam-7");
    expect(url).toContain("acknowledged=false");
    expect(url).toContain("limit=200");
    expect(url).not.toContain("plate=");

    expect(screen.getByText("cam-7").closest("a")?.getAttribute("href")).toBe(
      "/?camera=cam-7",
    );
    expect(screen.getByText("CRITICAL")).toBeTruthy();
    expect(screen.getByText(/stolen/)).toBeTruthy();
    expect(screen.getByText(/score 0\.98/)).toBeTruthy();
  });

  it("prepends a live SSE alert with a new marker, deduped on alert_id", async () => {
    render(<AlertsTable filters={NO_FILTERS} onFilters={vi.fn()} />);
    await screen.findAllByText("GJ01AB1234");
    const es = stream();

    act(() => es.emit("alert", LIVE));
    expect(screen.getAllByText("GJ05XY9999")).not.toHaveLength(0);
    expect(screen.getByText("new")).toBeTruthy();

    // Same alert_id delivered again — one row, not two.
    act(() => es.emit("alert", { ...LIVE }));
    expect(screen.getAllByText("cam-9")).toHaveLength(1);

    // And when a refresh returns the persisted copy, still one row.
    vi.mocked(fetch).mockResolvedValueOnce(json([STORED, { ...LIVE, id: 42, occurred_at: LIVE.raised_at }]));
    act(() => screen.getByText("Refresh").click());
    await waitFor(() => expect(screen.getAllByText("cam-9")).toHaveLength(1));
  });

  it("does not surface a live alert that fails the current filters", async () => {
    render(
      <AlertsTable filters={{ ...NO_FILTERS, camera: "cam-7" }} onFilters={vi.fn()} />,
    );
    await screen.findAllByText("GJ01AB1234");
    const es = stream();
    act(() => es.emit("alert", LIVE)); // camera cam-9, filter cam-7
    expect(screen.queryByText("cam-9")).toBeNull();
  });

  it("ack disables while in flight and flips the row on success", async () => {
    let resolveAck: (r: Response) => void = () => {};
    vi.mocked(fetch).mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === "POST" && url.endsWith("/api/bff/alerts/alert-1/ack")) {
        return new Promise<Response>((res) => {
          resolveAck = res;
        });
      }
      return Promise.resolve(json([STORED]));
    });

    render(<AlertsTable filters={NO_FILTERS} onFilters={vi.fn()} />);
    await screen.findAllByText("GJ01AB1234");

    const ackButton = screen.getByRole("button", { name: "ack" });
    act(() => ackButton.click());

    // In-flight: disabled, labelled so.
    const pending = await screen.findByRole("button", { name: "acking…" });
    expect((pending as HTMLButtonElement).disabled).toBe(true);

    await act(async () => {
      resolveAck(
        json({
          status: "acknowledged",
          ...STORED,
          acknowledged_at: new Date().toISOString(),
          acknowledged_by: "inspector.dave",
        }),
      );
    });

    await screen.findByText(/acked by inspector\.dave/);
    expect(screen.queryByRole("button", { name: "ack" })).toBeNull();
  });

  it("shows an ack error inline and leaves the button retryable", async () => {
    vi.mocked(fetch).mockImplementation((input, init) => {
      if (init?.method === "POST" && String(input).includes("/ack")) {
        return Promise.resolve(json({ detail: "alert is outside your own org subtree" }, 403));
      }
      return Promise.resolve(json([STORED]));
    });

    render(<AlertsTable filters={NO_FILTERS} onFilters={vi.fn()} />);
    await screen.findAllByText("GJ01AB1234");

    act(() => screen.getByRole("button", { name: "ack" }).click());

    await screen.findByText(/outside your own org subtree/);
    const retry = screen.getByRole("button", { name: "ack" });
    expect((retry as HTMLButtonElement).disabled).toBe(false);
  });

  it("renders an acked row with who and when, no ack button", async () => {
    vi.mocked(fetch).mockResolvedValue(
      json([
        {
          ...STORED,
          acknowledged_at: new Date(Date.now() - 60_000).toISOString(),
          acknowledged_by: "op-1",
        },
      ]),
    );
    render(<AlertsTable filters={NO_FILTERS} onFilters={vi.fn()} />);
    const cell = await screen.findByText(/acked by op-1/);
    expect(cell.textContent).toMatch(/ago/);
    expect(screen.queryByRole("button", { name: "ack" })).toBeNull();
  });
});
