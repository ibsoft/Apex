import type { NextRequest } from "next/server";

/**
 * Stream a multipart upload through to the Flask backend **without** the
 * `/be/api` rewrite. The Next rewrite clone-streams the body, which re-sends it
 * to gunicorn with no Content-Length (chunked); gunicorn closes on chunked
 * bodies, so the browser gets a connection reset ("Failed to fetch") for any
 * upload past ~100 KB. A route handler keeps the original Content-Length and
 * lets undici stream the exact byte count, which gunicorn accepts regardless of
 * size.
 *
 * Only the upload endpoints go through here; every other /be/api call keeps
 * using the rewrite, which is fine for the small request bodies they carry.
 */

export const BACKEND = process.env.APEX_BACKEND_URL ?? "http://127.0.0.1:5001";

export async function forwardUpload(
  req: NextRequest,
  upstreamPath: string,
): Promise<Response> {
  const contentType = req.headers.get("content-type");
  const contentLength = req.headers.get("content-length");
  const cookie = req.headers.get("cookie");
  const csrf = req.headers.get("x-apex-csrf");

  // Read the multipart body once as exact bytes, then re-send it with the
  // original Content-Length. Streaming `req.body` through the route handler
  // goes over Next's cloned-body PassThrough, which can serve a truncated
  // stream for uploads past ~1 MB; gunicorn sees a length mismatch and resets
  // the connection. A plain Buffer body with a real Content-Length is what
  // makes gunicorn accept any size (uploads are capped at 10 MB in Flask).
  let payload: ArrayBuffer;
  try {
    payload = await req.arrayBuffer();
  } catch (err) {
    return new Response(
      JSON.stringify({ error: `could not read upload body: ${(err as Error).message}` }),
      { status: 400, headers: { "content-type": "application/json" } },
    );
  }
  const declared = contentLength ? Number(contentLength) : payload.byteLength;
  if (Number.isFinite(declared) && declared > 0 && payload.byteLength !== declared) {
    return new Response(
      JSON.stringify({
        error: `upload truncated by the frontend (${payload.byteLength} of ${declared} bytes)`,
      }),
      { status: 413, headers: { "content-type": "application/json" } },
    );
  }

  const headers = new Headers();
  if (contentType) headers.set("content-type", contentType);
  if (cookie) headers.set("cookie", cookie);
  if (csrf) headers.set("x-apex-csrf", csrf);
  headers.set("content-length", String(payload.byteLength));

  let resp: Response;
  try {
    resp = await fetch(`${BACKEND}${upstreamPath}`, {
      method: "POST",
      headers,
      body: Buffer.from(payload),
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: `backend upload relay failed: ${(err as Error).message}` }),
      { status: 502, headers: { "content-type": "application/json" } },
    );
  }

  const out = await resp.arrayBuffer();
  return new Response(out, {
    status: resp.status,
    headers: {
      "content-type": resp.headers.get("content-type") ?? "application/json",
    },
  });
}