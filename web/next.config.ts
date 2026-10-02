import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  poweredByHeader: false,
  // client router cache: going back to a page (or reopening it) within 30 s shows it instantly instead of waiting for the server
  experimental: { staleTimes: { dynamic: 30 } },
};

export default nextConfig;
