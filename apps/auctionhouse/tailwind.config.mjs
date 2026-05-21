import preset from "@nl06/design-system/tailwind-preset";

/** @type {import('tailwindcss').Config} */
export default {
  presets: [preset],
  content: [
    "./src/**/*.{astro,html,md,mdx,ts,tsx,js,jsx}",
    "../../packages/design-system/src/**/*.{astro,ts,tsx,js,jsx}",
  ],
};
