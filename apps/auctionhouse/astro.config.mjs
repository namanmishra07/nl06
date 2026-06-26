import { defineConfig } from "astro/config";
import tailwind from "@astrojs/tailwind";
import sitemap from "@astrojs/sitemap";

export default defineConfig({
  site: "https://auctionhouse.nl06.com",
  trailingSlash: "never",
  build: { format: "file" },
  integrations: [
    tailwind({ applyBaseStyles: false }),
    // Keep the private, Access-gated /watch dashboard out of the public sitemap.
    sitemap({ filter: (page) => !page.includes("/watch") }),
  ],
});
