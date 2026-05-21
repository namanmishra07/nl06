import { defineCollection, z } from "astro:content";
import { glob } from "astro/loaders";

const chains = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/chains" }),
  schema: z.object({
    /** The chain's title — usually the macro shift in one line. */
    title: z.string(),
    status: z.enum(["LIVE", "KILLED", "UNRESOLVED"]),
    last_updated: z.coerce.date(),
    /** The macro shift restated. Can repeat the title; kept separate for structure. */
    macro_shift: z.string(),
    /** Ordered chain steps. */
    steps: z.array(z.string()).min(1),
    /** One-line verdict — visible on the index for LIVE/UNRESOLVED. */
    verdict_reason: z.string().optional(),
    /** One-line kill reason — visible in the KILLED archive. */
    killed_reason: z.string().optional(),
    draft: z.boolean().default(false),
  }),
});

export const collections = { chains };
