import { defineConfig } from "astro/config";
import tailwind from "@astrojs/tailwind";
import sitemap from "@astrojs/sitemap";

export default defineConfig({
  site: "https://auctionhouse.nl06.com",
  trailingSlash: "never",
  build: { format: "file" },
  integrations: [
    tailwind({ applyBaseStyles: false }),
    sitemap(),
  ],
});
