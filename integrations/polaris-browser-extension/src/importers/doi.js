import { canonicalDoi } from "../shared/normalization.js";

const DOI_PATTERN = /10\.\d{4,9}\/[-._;()/:a-z0-9]+/gi;

export function parseDoiText(text) {
  const found = String(text || "").match(DOI_PATTERN) || [];
  const dois = [];
  const seen = new Set();
  for (const value of found) {
    const doi = canonicalDoi(value);
    if (!doi || seen.has(doi)) continue;
    seen.add(doi);
    dois.push({ doi, title: doi, sources: ["doi-list"] });
  }
  return dois;
}
