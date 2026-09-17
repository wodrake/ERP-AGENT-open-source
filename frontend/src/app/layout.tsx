import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "智能采购助手 - ERP Agent",
  description: "基于 Harness Engineering 架构的智能采购助手",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // Browser extensions may add attributes to <html> before hydration (for
    // example Immersive Translate). Suppress only this root-level mismatch;
    // keep hydration checks enabled for application content below it.
    <html lang="zh-CN" className="h-full antialiased" suppressHydrationWarning>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
