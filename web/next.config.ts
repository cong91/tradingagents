import type { NextConfig } from "next";

// The browser calls /api/* on this Next.js server, which proxies to FastAPI.
// Local development uses 127.0.0.1:8000; docker-compose sets API_INTERNAL_URL
// to the api service so the container reaches it over the compose network.
const apiBase = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Verification builds go to their own directory so a build never fights the
  // dev server over .next (Next 16 dropped the CLI --dist-dir flag). The
  // browser fixture test builds with NEXT_DIST_DIR=.next-verify and serves that
  // output; the default stays .next for dev and normal production builds.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  async redirects() {
    // /pipeline đã gộp vào trang Hoạt động bot; /scanner cũng vậy. Bookmark cũ
    // của người dùng phải về đích mới thay vì 404 (temporary: có thể đổi lại).
    return [
      { source: "/pipeline", destination: "/cycles", permanent: false },
      { source: "/scanner", destination: "/cycles", permanent: false },
    ];
  },
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
