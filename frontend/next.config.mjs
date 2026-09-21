/** @type {import('next').NextConfig} */
const nextConfig = {
  distDir: process.env.NEXT_DIST_DIR || ".next",
  async rewrites() {
    // Dev proxy: /be/api/* -> Flask backend /api/* (see lib/api.ts).
    return [{ source: "/be/api/:path*", destination: "http://127.0.0.1:5001/api/:path*" }];
  },
  experimental: {
    optimizePackageImports: ["three"],
  },
};

export default nextConfig;