#!/usr/bin/env node
/**
 * compress-plates.mjs — generate lossless WebP next to each PNG under
 * apps/gs/public/plates/ and apps/main/public/hero/, so the gs site
 * ships a smaller payload while preserving every pixel.
 *
 * Pages link to the original PNG as "open at full resolution"; the
 * <picture> element serves the WebP to modern browsers and falls back
 * to PNG for the few that don't support WebP.
 *
 * Idempotent: skips a target whose mtime is newer than the source.
 *
 * Usage:
 *   node /website/scripts/compress-plates.mjs            # generate missing
 *   node /website/scripts/compress-plates.mjs --force    # regenerate all
 */

import { readdir, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import { extname, join, basename } from "node:path";
import sharp from "sharp";

const TARGETS = [
  "/website/apps/gs/public/plates",
  "/website/apps/main/public/hero",
];

const FORCE = process.argv.includes("--force");

const fmtBytes = (n) =>
  n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(2)} MB` : `${(n / 1024).toFixed(0)} KB`;

async function compressOne(srcPath) {
  const dstPath = srcPath.replace(/\.png$/i, ".webp");
  if (!FORCE && existsSync(dstPath)) {
    const [s, d] = await Promise.all([stat(srcPath), stat(dstPath)]);
    if (d.mtimeMs >= s.mtimeMs) return { skipped: true, srcPath, dstPath };
  }
  const srcStat = await stat(srcPath);
  await sharp(srcPath)
    // Lossless WebP. effort 6 = strongest compression search (slower
    // encode, but encode is one-shot; runtime cost is zero).
    .webp({ lossless: true, effort: 6 })
    .toFile(dstPath);
  const dstStat = await stat(dstPath);
  return {
    skipped: false,
    srcPath,
    dstPath,
    srcSize: srcStat.size,
    dstSize: dstStat.size,
    ratio: dstStat.size / srcStat.size,
  };
}

async function processDir(dir) {
  if (!existsSync(dir)) {
    console.log(`compress-plates: ${dir} doesn't exist; skipping`);
    return { processed: 0, skipped: 0, srcTotal: 0, dstTotal: 0 };
  }
  const entries = await readdir(dir);
  const pngs = entries.filter((n) => extname(n).toLowerCase() === ".png");
  let processed = 0, skipped = 0, srcTotal = 0, dstTotal = 0;
  for (const name of pngs) {
    const src = join(dir, name);
    const result = await compressOne(src);
    if (result.skipped) {
      skipped++;
      console.log(`  · ${basename(src).padEnd(70)}  (already current, skipped)`);
    } else {
      processed++;
      srcTotal += result.srcSize;
      dstTotal += result.dstSize;
      console.log(
        `  ✓ ${basename(src).padEnd(70)}  ${fmtBytes(result.srcSize)} → ` +
        `${fmtBytes(result.dstSize)}  (${((1 - result.ratio) * 100).toFixed(0)}% smaller)`
      );
    }
  }
  return { processed, skipped, srcTotal, dstTotal };
}

let totals = { processed: 0, skipped: 0, srcTotal: 0, dstTotal: 0 };
for (const dir of TARGETS) {
  console.log(`\n${dir}/`);
  const r = await processDir(dir);
  totals.processed += r.processed;
  totals.skipped += r.skipped;
  totals.srcTotal += r.srcTotal;
  totals.dstTotal += r.dstTotal;
}

console.log(
  `\ncompress-plates: ${totals.processed} compressed, ${totals.skipped} already current. ` +
  (totals.processed > 0
    ? `Saved ${fmtBytes(totals.srcTotal - totals.dstTotal)} ` +
      `(${((1 - totals.dstTotal / totals.srcTotal) * 100).toFixed(0)}% reduction).`
    : "")
);
