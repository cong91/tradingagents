import type { NextConfig } from "next";

// The browser calls /api/* on this Next.js server, which proxies to FastAPI.
// Local development uses 127.0.0.1:8000; docker-compose sets API_INTERNAL_URL
// to the api service so the container reaches it over the compose network.
const apiBase = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiBase}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
