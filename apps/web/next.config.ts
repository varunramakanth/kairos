import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // A minimal server bundle for the container image (infra/docker/web.Dockerfile).
  output: "standalone",
  cacheComponents: true,
  partialPrefetching: true,
  // The service worker must never be served stale (Next.js PWA guide).
  async headers() {
    return [
      {
        source: "/sw.js",
        headers: [
          {
            key: "Cache-Control",
            value: "no-cache, no-store, must-revalidate",
          },
          {
            key: "Content-Type",
            value: "application/javascript; charset=utf-8",
          },
          {
            key: "Content-Security-Policy",
            value: "default-src 'self'; script-src 'self'",
          },
        ],
      },
    ];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
