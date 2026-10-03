/** @type {import('next').NextConfig} */
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export default {
  // Same-origin /api/v1 calls are proxied to FastAPI, so no CORS in the browser.
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${BACKEND_URL}/api/v1/:path*` }];
  },
  experimental: { proxyTimeout: 180_000 }, // multimodal ingestion can take a while
};
