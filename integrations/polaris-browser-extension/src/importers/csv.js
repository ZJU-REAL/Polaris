import { cleanText } from "../shared/normalization.js";

function parseRows(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  const source = String(text || "").replace(/^\uFEFF/, "");
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"') quoted = true;
    else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else field += char;
  }
  row.push(field.replace(/\r$/, ""));
  if (row.some((value) => value.trim())) rows.push(row);
  return rows;
}

const COLUMN_ALIASES = {
  id: ["id", "paperid", "paper_id"],
  doi: ["doi", "digitalobjectidentifier"],
  title: ["title", "题名", "标题"],
  authors: ["authors", "author", "作者"],
  year: ["year", "publicationyear", "年份"],
  venue: ["venue", "journal", "publication", "期刊"],
  publisher: ["publisher", "出版社"],
  url: ["url", "link", "articleurl"],
  pdfUrl: ["pdf", "pdfurl", "pdf_url"],
};

function normalizeHeader(value) {
  return cleanText(value, 100).toLocaleLowerCase().replace(/[\s_-]+/g, "");
}

function columnIndex(headers, aliases) {
  return headers.findIndex((header) => aliases.includes(header));
}

export function parseCsv(text) {
  const rows = parseRows(text);
  if (!rows.length) return [];
  const headers = rows[0].map(normalizeHeader);
  const hasNamedHeader = Object.values(COLUMN_ALIASES).flat().some((alias) => headers.includes(normalizeHeader(alias)));
  if (!hasNamedHeader) {
    return rows.map((row, index) => ({ doi: cleanText(row[0], 512), title: cleanText(row[1], 1000) || cleanText(row[0], 512), id: `csv-${index + 1}`, sources: ["csv"] })).filter((item) => item.doi || item.title);
  }
  const indexes = Object.fromEntries(Object.entries(COLUMN_ALIASES).map(([key, aliases]) => [key, columnIndex(headers, aliases.map(normalizeHeader))]));
  const valueAt = (row, key) => indexes[key] >= 0 ? cleanText(row[indexes[key]], key === "title" ? 1000 : 512) : "";
  return rows.slice(1).map((row, index) => ({
    id: valueAt(row, "id") || `csv-${index + 1}`,
    doi: valueAt(row, "doi"),
    title: valueAt(row, "title") || valueAt(row, "doi") || `CSV paper ${index + 1}`,
    authors: valueAt(row, "authors").split(/\s*(?:;|\band\b)\s*/i).filter(Boolean),
    year: Number.parseInt(valueAt(row, "year"), 10) || null,
    venue: valueAt(row, "venue") || null,
    publisher: valueAt(row, "publisher") || null,
    url: valueAt(row, "url") || null,
    pdfUrl: valueAt(row, "pdfUrl") || null,
    sources: ["csv"],
  })).filter((item) => item.doi || item.title);
}
