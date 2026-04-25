import type { Metadata } from "next";
import { Open_Sans, Poppins } from "next/font/google";
import "./globals.css";
import { Providers } from "@/app/providers";
import type { ReactNode } from "react";

const openSans = Open_Sans({
  subsets: ["latin"],
  variable: "--font-open-sans",
});

const poppins = Poppins({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-poppins",
});

export const metadata: Metadata = {
  title: "Kite Algo",
  description: "Open-source algorithmic trading terminal for Indian markets.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning className="h-full">
      <body className={`${openSans.variable} ${poppins.variable} min-h-full bg-background text-foreground antialiased`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
