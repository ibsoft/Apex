/** @type {import('next').NextConfig} */
const nextConfig = {
  distDir: process.env.NEXT_DIST_DIR || ".next",
  async rewrites() {
    // Dev proxy: /be/api/* -> Flask backend /api/* (see lib/api.ts).
    return [{ source: "/be/api/:path*", destination: "http://127.0.0.1:5001/api/:path*" }];
  },
  experimental: {
    optimizePackageImports: ["three"],
    // Self-checks allow 120s for backend tests and 300s for the frontend build.
    // Keep the proxy open long enough to return their diagnostics to the UI.
    proxyTimeout: 480000,
  },
};

export default nextConfig;
