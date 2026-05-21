import rss from "@astrojs/rss";
import { getCollection } from "astro:content";
import type { APIContext } from "astro";

export async function GET(context: APIContext) {
  const notes = (await getCollection("notes"))
    .filter((n) => !n.data.draft)
    .sort((a, b) => b.data.date.getTime() - a.data.date.getTime());

  return rss({
    title: "reading.nl06",
    description: "Notes on what I'm reading.",
    site: context.site!,
    items: notes.map((n) => ({
      title:       n.data.title,
      pubDate:     n.data.date,
      description: n.data.blurb,
      link:        `/${n.id}`,
    })),
    customData: `<language>en</language>`,
  });
}
