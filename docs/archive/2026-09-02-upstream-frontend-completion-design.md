# Polaris upstream frontend completion

## Scope

This change set closes three frontend gaps left after the literature backend was merged:

1. expose document-processing runtime settings and the MinerU credential pool to administrators;
2. make the existing Unpaywall resolver selectable and testable from literature settings;
3. keep the literature list/detail workspace usable when the main content area is 1025-1280 pixels wide.

Each item is delivered as an independent pull request based on the same upstream commit. None of the branches depends on another branch.

## Document-processing administration

The administrator page gains a `Document processing` tab. It calls the existing `/api/admin/settings/document-processing` endpoints and does not introduce a second configuration store.

The page contains two bounded sections:

- runtime policy: MinerU enablement, API root, timeout, polling interval, retries, concurrency, and PyMuPDF fallback;
- MinerU credential pool: add, edit, enable/disable, delete, and connection-test actions.

Secrets remain write-only. Editing a credential with an empty secret preserves the stored value. General settings are saved explicitly; credential actions persist immediately, matching the literature provider settings.

## Unpaywall settings

Unpaywall remains an OA resolver rather than a primary bibliographic search source. It is shown separately from selectable search sources and can be tested with a DOI. This prevents an empty resolver adapter from consuming candidate-search quota or appearing as a zero-result database.

No credential pool is added because the current resolver uses the contact email from server settings. Resolver health is persisted with the other provider health records.

## Responsive literature workspace

The existing `.content` element already establishes a named inline-size container. The fix uses that container as the breakpoint source so the UI reacts to the width that is actually available after the application sidebar and docked panels are accounted for.

When the main area is at most 1180 pixels wide:

- list/detail workspaces stack vertically;
- the list receives a bounded height and a bottom divider;
- page tabs become a single-line horizontal scroller, preventing labels from collapsing into one character per line.

The existing viewport rules remain as fallbacks for browsers without container-query support. Mobile behavior is unchanged.

## Verification

Each branch must pass its focused tests, the complete frontend test suite, and the production build. The Unpaywall branch also runs the administrator-settings and literature-runtime backend tests. The responsive branch is checked at desktop, 1025-1280 pixel, and mobile widths with screenshots and overflow measurements.
