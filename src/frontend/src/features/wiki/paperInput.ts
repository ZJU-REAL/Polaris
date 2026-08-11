const PAPER_INPUT_SEPARATOR_RE = /[\s,，;；]+/u;

/** Split pasted paper identifiers on whitespace, commas, or semicolons. */
export function splitPaperInput(raw: string): string[] {
  return raw
    .split(PAPER_INPUT_SEPARATOR_RE)
    .map((value) => value.trim())
    .filter(Boolean);
}
