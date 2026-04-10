import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["192.168.0.128", "localhost", "127.0.0.1"],
  turbopack: {
    root: process.cwd(),
  },
  async rewrites() {
    const backend = process.env.BACKEND_INTERNAL_URL ?? "http://localhost:18777";
    return [
      {
        source: "/api/:path*",
        destination: `${backend}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
