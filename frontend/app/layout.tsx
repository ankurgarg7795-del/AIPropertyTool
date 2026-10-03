import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "AIPropertyTool",
  description: "AI-native, free-to-list real estate: conversational search, zero-form listings, 24/7 concierge.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-IN">
      <body>
        <nav className="nav">
          <Link href="/" className="brand">AIPropertyTool</Link>
          <div>
            <Link href="/">Find a home</Link>
            <Link href="/sell">List for free</Link>
          </div>
        </nav>
        <main>{children}</main>
      </body>
    </html>
  );
}
