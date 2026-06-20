import type { NextConfig } from "next";

// Fully client-side app: no server, no API routes. `output: "export"` emits a
// static bundle (out/) that any free static host (Cloudflare Pages, Vercel,
// GitHub Pages) can serve. Email fetch + LLM all run in the browser.
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
