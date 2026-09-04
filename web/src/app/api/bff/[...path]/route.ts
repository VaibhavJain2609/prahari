import { NextRequest } from "next/server";

const BFF_URL = process.env.PRAHARI_BFF_URL ?? "http://localhost:8001";

// The one place the browser's fetches leave this Next.js server. Every BFF
// call — auth, cameras, orgs, routes, audit, the SSE alert stream — goes
// through here rather than each getting its own route.ts, so the browser
// only ever knows one origin (this one) and CORS never comes up. Cookies
// (session) and the X-Purpose-Code header pass through in both directions;
// everything else about the request (method, query, body, content-type) is
// forwarded verbatim.
async function proxy(request: NextRequest, path: string[]): Promise<Response> {
  const upstream = new URL(`/api/v1/${path.join("/")}`, BFF_URL);
  request.nextUrl.searchParams.forEach((value, key) => upstream.searchParams.append(key, value));

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const cookie = request.headers.get("cookie");
  if (cookie) headers.set("cookie", cookie);
  const authorization = request.headers.get("authorization");
  if (authorization) headers.set("authorization", authorization);
  const purposeCode = request.headers.get("x-purpose-code");
  if (purposeCode) headers.set("x-purpose-code", purposeCode);

  const hasBody = !["GET", "HEAD"].includes(request.method);

  let upstreamResponse: Response;
  try {
    upstreamResponse = await fetch(upstream, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      cache: "no-store",
    });
  } catch {
    return Response.json({ error: "bff unreachable", bff_url: BFF_URL }, { status: 502 });
  }

  const responseHeaders = new Headers();
  for (const name of ["content-type", "cache-control", "content-disposition"]) {
    const value = upstreamResponse.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  // Node's fetch (undici) exposes multi-value Set-Cookie only through
  // getSetCookie() — .get("set-cookie") would silently collapse a login's
  // one cookie into a comma-joined string a browser cannot parse back apart.
  const setCookies = upstreamResponse.headers.getSetCookie?.() ?? [];
  for (const value of setCookies) responseHeaders.append("set-cookie", value);

  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    headers: responseHeaders,
  });
}

type RouteParams = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, { params }: RouteParams) {
  return proxy(request, (await params).path);
}
export async function POST(request: NextRequest, { params }: RouteParams) {
  return proxy(request, (await params).path);
}
export async function PATCH(request: NextRequest, { params }: RouteParams) {
  return proxy(request, (await params).path);
}
export async function DELETE(request: NextRequest, { params }: RouteParams) {
  return proxy(request, (await params).path);
}
