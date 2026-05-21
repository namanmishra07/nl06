import { defineCollection, z } from "astro:content";
import { file } from "astro/loaders";

/**
 * Single source of truth for every capture the gs rig has produced — both
 * successful decodes (with images) and failed attempts (CADU = 0). Mirrors
 * the schema of `/satellite/.processed-iq-log.ndjson` on the NAS so the
 * post-decode script can write here directly without translation.
 *
 * To add a new capture: append one object to src/content/plates.json.
 * Required: id, captured_at, satellite, band, frequency_mhz, outcome.
 * Everything else is optional (failed captures have no image/pipeline/etc).
 */
const plates = defineCollection({
  loader: file("src/content/plates.json"),
  schema: z.object({
    /** Editorial title — present for plates with an image; omitted on failures. */
    title: z.string().optional(),
    satellite: z.string(),
    channel: z.string().optional(),
    captured_at: z.coerce.date(),
    aos: z.coerce.date().optional(),
    los: z.coerce.date().optional(),
    duration_s: z.number().optional(),
    band: z.enum(["L", "VHF"]),
    frequency_mhz: z.number(),
    snr_db: z.number().nullable().optional(),
    outcome: z.enum(["success", "partial", "fail_no_cadu", "not_decoded"]),
    /** CADU bytes produced; 0 for fail_no_cadu. */
    cadu_bytes: z.number().optional(),
    /** Public asset path under /plates/. Missing → not rendered as a plate, only logged. */
    image: z.string().optional(),
    caption: z.string().optional(),
    pipeline: z.string().optional(),
    colorised: z.boolean().default(false),
    /** Free-text operational note (failures use this, successes may omit). */
    notes: z.string().optional(),
    /** Provenance hash from .processed-iq-log.ndjson. Optional; null is fine. */
    iq_sha256_first_mb: z.string().nullable().optional(),
    /** Pin this plate as the cover of the gs home page. At most one. */
    hero: z.boolean().default(false),
    draft: z.boolean().default(false),
  }),
});

export const collections = { plates };
