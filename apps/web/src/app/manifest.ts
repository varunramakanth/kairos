import type { MetadataRoute } from "next";

/** Installable as an app (Add to Home Screen / Install), opening on the chat. */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Kairos",
    short_name: "Kairos",
    description: "An always-on personal agent",
    start_url: "/",
    scope: "/",
    display: "standalone",
    background_color: "#0a0a0a",
    theme_color: "#0a0a0a",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
      {
        src: "/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
    shortcuts: [
      { name: "New chat", url: "/" },
      { name: "Approvals", url: "/inbox" },
      { name: "Tasks & watches", url: "/tasks" },
    ],
  };
}
