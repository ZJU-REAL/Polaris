import { cleanText } from "../shared/normalization.js";

function readBalanced(source, start, open, close) {
  let depth = 0;
  let quoted = false;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (char === '"' && source[index - 1] !== "\\") quoted = !quoted;
    if (quoted) continue;
    if (char === open) depth += 1;
    else if (char === close) {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return -1;
}

function parseFields(body) {
  const fields = {};
  let cursor = body.indexOf(",") + 1;
  while (cursor > 0 && cursor < body.length) {
    while (cursor < body.length && /[\s,]/.test(body[cursor])) cursor += 1;
    const keyMatch = body.slice(cursor).match(/^([a-zA-Z][\w-]*)\s*=\s*/);
    if (!keyMatch) break;
    const key = keyMatch[1].toLowerCase();
    cursor += keyMatch[0].length;
    let value = "";
    if (body[cursor] === "{") {
      const end = readBalanced(body, cursor, "{", "}");
      if (end < 0) break;
      value = body.slice(cursor + 1, end);
      cursor = end + 1;
    } else if (body[cursor] === '"') {
      cursor += 1;
      const start = cursor;
      while (cursor < body.length && !(body[cursor] === '"' && body[cursor - 1] !== "\\")) cursor += 1;
      value = body.slice(start, cursor);
      cursor += 1;
    } else {
      const end = body.indexOf(",", cursor);
      value = body.slice(cursor, end < 0 ? body.length : end);
      cursor = end < 0 ? body.length : end + 1;
    }
    fields[key] = cleanText(value.replace(/[{}]/g, ""), key === "title" ? 1000 : 1000);
  }
  return fields;
}

export function parseBibtex(text) {
  const source = String(text || "");
  const records = [];
  let cursor = 0;
  while (cursor < source.length) {
    const match = source.slice(cursor).match(/@([a-zA-Z]+)\s*([({])/);
    if (!match) break;
    const start = cursor + match.index;
    const openIndex = start + match[0].lastIndexOf(match[2]);
    const close = match[2] === "{" ? "}" : ")";
    const end = readBalanced(source, openIndex, match[2], close);
    if (end < 0) break;
    const body = source.slice(openIndex + 1, end);
    const citationKey = cleanText(body.slice(0, body.indexOf(",")), 160);
    const fields = parseFields(body);
    records.push({
      id: citationKey || `bib-${records.length + 1}`,
      doi: fields.doi || "",
      title: fields.title || fields.doi || citationKey || `BibTeX paper ${records.length + 1}`,
      authors: (fields.author || "").split(/\s+and\s+/i).map((value) => cleanText(value, 240)).filter(Boolean),
      year: Number.parseInt(fields.year || "", 10) || null,
      venue: fields.journal || fields.booktitle || null,
      publisher: fields.publisher || null,
      url: fields.url || null,
      pdfUrl: fields.file?.match(/https?:\/\/[^\s;}]+/i)?.[0] || null,
      sources: ["bibtex"],
    });
    cursor = end + 1;
  }
  return records;
}
