/** @type {import('tailwindcss').Config} */
export default {
  theme: {
    colors: {
      transparent: "transparent",
      current: "currentColor",
      paper:      "var(--color-paper)",
      "paper-dim": "var(--color-paper-dim)",
      ink:        "var(--color-ink)",
      "ink-soft": "var(--color-ink-soft)",
      "ink-muted": "var(--color-ink-muted)",
      rule:       "var(--color-rule)",
      accent:     "var(--color-accent)",
      "accent-hover": "var(--color-accent-hover)",
    },
    fontFamily: {
      serif: "var(--font-serif)",
      sans:  "var(--font-sans)",
      mono:  "var(--font-mono)",
    },
    fontSize: {
      meta:    ["var(--type-meta)",    { lineHeight: "var(--leading-normal)" }],
      small:   ["var(--type-small)",   { lineHeight: "var(--leading-normal)" }],
      body:    ["var(--type-body)",    { lineHeight: "var(--leading-prose)" }],
      h4:      ["var(--type-h4)",      { lineHeight: "var(--leading-snug)" }],
      h3:      ["var(--type-h3)",      { lineHeight: "var(--leading-snug)" }],
      h2:      ["var(--type-h2)",      { lineHeight: "var(--leading-snug)" }],
      h1:      ["var(--type-h1)",      { lineHeight: "var(--leading-tight)" }],
      display: ["var(--type-display)", { lineHeight: "var(--leading-tight)", letterSpacing: "var(--tracking-tight)" }],
    },
    extend: {
      maxWidth: {
        prose:     "var(--width-prose)",
        container: "var(--width-container)",
        wide:      "var(--width-wide)",
        full:      "var(--width-full)",
      },
      spacing: {
        page: "var(--space-6)",
      },
    },
  },
  corePlugins: {
    // disable utilities we explicitly don't want to encourage
    boxShadow: false,
    dropShadow: false,
    gradientColorStops: false,
    backgroundImage: false,
  },
  plugins: [],
};
