import { NextRequest, NextResponse } from "next/server";

const REGISTRY_URL = process.env.PRAHARI_REGISTRY_URL ?? "http://localhost:8000";

// Proxied server-side so the browser never talks to registry directly -
// keeps registry's origin off the client and sidesteps CORS entirely.
export async function GET(request: NextRequest) {
  const upstream = new URL("/api/v1/cameras/geojson", REGISTRY_URL);
  const bbox = request.nextUrl.searchParams.get("bbox");
  if (bbox) upstream.searchParams.set("bbox", bbox);

  let res: Response;
  try {
    res = await fetch(upstream, { cache: "no-store" });
  } catch {
    return NextResponse.json(
      { error: "registry unreachable", registry_url: REGISTRY_URL },
      { status: 502 },
    );
  }

  if (!res.ok) {
    return NextResponse.json(
      { error: "registry returned an error", status: res.status },
      { status: res.status },
    );
  }

  return NextResponse.json(await res.json());
}
