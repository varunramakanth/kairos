import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";
import { AuthGate } from "@/components/auth-gate";
import { CommandPalette } from "@/components/command-palette";
import { MobileBar, ShellProvider } from "@/components/shell";
import { ThreadSidebar } from "@/components/thread-sidebar";
import { Tour } from "@/components/tour";
import { THEME_SCRIPT } from "@/lib/theme";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Kairos",
  description: "Always-on personal agent",
  applicationName: "Kairos",
  appleWebApp: { capable: true, title: "Kairos", statusBarStyle: "default" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      // The theme script sets the dark class before React hydrates.
      suppressHydrationWarning
    >
      <head>
        {/* biome-ignore lint/security/noDangerouslySetInnerHtml: a fixed, first-party script that must run before paint */}
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="flex h-full">
        {/* The gate and the sidebar read the current path: request-time data. */}
        <Suspense
          fallback={
            <aside className="hidden w-64 shrink-0 border-r bg-sidebar md:block" />
          }
        >
          <AuthGate>
            <ShellProvider>
              <ThreadSidebar />
              <main className="flex min-h-0 min-w-0 flex-1 flex-col">
                <MobileBar />
                {/* Pages fill this area; long ones scroll inside it. */}
                <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
                  {children}
                </div>
              </main>
              <CommandPalette />
              <Tour />
            </ShellProvider>
          </AuthGate>
        </Suspense>
      </body>
    </html>
  );
}
