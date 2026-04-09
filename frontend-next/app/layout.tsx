import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/app/providers";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Frontend Next Shell",
  description: "Terminal-style operator shell scaffold for the frontend-next worktree.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning className="h-full">
      <body className="min-h-full bg-background text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
