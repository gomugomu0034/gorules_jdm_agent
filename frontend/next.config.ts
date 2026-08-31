import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${process.env.BACKEND_URL ?? 'http://backend:8000'}/api/:path*`,
      },
    ]
  },
};

export default nextConfig;
