import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Internship Scanner",
  description: "Dashboard for the Gmail internship scanner.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
