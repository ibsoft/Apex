/** @type {import('next').NextConfig} */
const nextConfig = {
  distDir: process.env.NEXT_DIST_DIR || ".next",
  async rewrites() {
    // Dev proxy: /be/api/* -> Flask backend /api/* (see lib/api.ts).
    return [{ source: "/be/api/:path*", destination: "http://127.0.0.1:5001/api/:path*" }];
  },
  async headers() {
    return [
      {
        // The service worker must be revalidated on every load, and it needs a
        // JavaScript content type. A stale worker pins the whole app to an old
        // build until the cache is cleared by hand from devtools, which is a
        // confusing failure to debug from the UI side. Next already sends
        // max-age=0 for public/ files; this makes it explicit and survives a
        // proxy that would otherwise add its own caching.
        source: "/sw.js",
        headers: [
          { key: "Content-Type", value: "application/javascript; charset=utf-8" },
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
      {
        // The manifest changes when an icon or a shortcut changes, and a cached
        // manifest is what makes the install prompt offer a stale icon.
        source: "/manifest.webmanifest",
        headers: [
          { key: "Content-Type", value: "application/manifest+json" },
          { key: "Cache-Control", value: "public, max-age=0, must-revalidate" },
        ],
      },
    ];
  },
  experimental: {
    optimizePackageImports: ["three"],
    // Self-checks allow 120s for backend tests and 300s for the frontend build.
    // Keep the proxy open long enough to return their diagnostics to the UI.
    proxyTimeout: 480000,
  },
};

export default nextConfig;
