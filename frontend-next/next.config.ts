import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["192.168.0.128", "localhost", "127.0.0.1"],
  turbopack: {
    root: process.cwd(),
  },
  async rewrites() {
    const backend = process.env.BACKEND_INTERNAL_URL ?? "http://localhost:18777";
    const marketRuntime = process.env.MARKET_RUNTIME_URL ?? process.env.NEXT_PUBLIC_MARKET_RUNTIME_URL ?? "http://localhost:8780";
    return [
      {
        source: "/api/:path*",
        destination: `${backend}/api/:path*`,
      },
      {
        source: "/ws/:path*",
        destination: `${marketRuntime}/ws/:path*`,
      },
    ];
  },
};

export default nextConfig;
