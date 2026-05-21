import { defineCollection, z } from "astro:content";
import { glob } from "astro/loaders";

const notes = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/notes" }),
  schema: z.object({
    title: z.string(),
    date: z.coerce.date(),
    source_type: z.enum(["book", "paper", "post", "other"]),
    source_link: z.string().url().optional(),
    source_author: z.string().optional(),
    /** Optional one-line summary for the index. */
    blurb: z.string().optional(),
    /** Drafts are excluded from index + RSS at build time. */
    draft: z.boolean().default(false),
  }),
});

export const collections = { notes };
