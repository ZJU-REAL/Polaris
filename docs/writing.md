# Paper writing

The Paper Writer is an online, multi-file LaTeX workspace: conference templates, a CodeMirror
editor with real-time collaboration, server-side compilation with a live PDF preview, and an AI
drafting agent that writes the paper section by section — under hard guardrails that keep every
number and every citation tied to something real.

The page lives in the topic workspace under **Paper Writer**.

## How it works

### Manuscripts and templates

A manuscript is a self-contained LaTeX project inside a topic: a file tree of `.tex` sources,
`references.bib`, style files, and uploaded assets. When you create one you pick a template:

- **Built-in**: NeurIPS 2026, ICLR 2026, and ACL, shipped with the platform. Their style files
  (`.sty`/`.cls`/`.bst`) are read-only so you cannot accidentally break the venue format.
- **Official**: additional venue packs downloadable on demand (the picker shows
  "Official · not downloaded" until fetched).
- **Custom**: upload any template as a zip ("Upload zip"); the main `.tex` file is detected
  automatically and binary assets are stored as read-only files.

Each manuscript has a **main file** (the compile entry point) and a **compiler** — `tectonic` by
default (always available in the Docker image), with `pdflatex` / `xelatex` / `lualatex` offered when
TeX Live's `latexmk` is installed on the server. Both are switchable in the editor top bar,
Overleaf-style.

<!-- screenshot: New manuscript modal with the template picker showing built-in, official and custom templates -->

### The editor

The editor page is three panes: file tree, CodeMirror editor, and the PDF preview.

- **Real-time collaboration.** Every text file is a CRDT document synced over WebSocket — multiple
  people (and the AI) can edit the same file at once, with peer cursors and an online-collaborators
  list in the top bar. Changes snapshot to the database within ~2 seconds, so compiles and the REST
  API always see the latest text. If your connection drops, edits are kept locally and merged when
  the socket reconnects ("Connection lost — changes are kept locally and will sync after
  reconnecting").
- **Compile and preview.** Press **⌘S** or click **Compile**. Compilation runs server-side with the
  selected engine and streams back a versioned PDF plus parsed diagnostics — undefined citations,
  undefined references, LaTeX errors with `file:line`, and overfull-box warnings. Clicking a
  diagnostic jumps to the file and line.
- **Files.** Create files and folders, rename, upload (binary uploads such as images become
  read-only assets), and pick any `.tex` as the main file. `figures/` is reserved: experiment figures
  are injected there at compile time.
- **Fact pack drawer.** The **Fact pack** button shows the manuscript's fact sources at a glance:
  the idea, hypotheses with their verified/falsified status, every metric from every experiment run,
  the QC-checked figures, and the citable knowledge-base entries with their bibkeys.

<!-- screenshot: Writer editor with the file tree, CodeMirror pane with a collaborator cursor, and the compiled PDF preview -->

### The fact pack: where the guardrails come from

When a manuscript is created (and automatically refreshed before every AI drafting run) the platform
assembles a **fact pack** from real data:

| Section | Source |
| --- | --- |
| `idea` | The linked idea's title and summary |
| `hypotheses` | The linked experiment's hypotheses with status and evidence |
| `metrics` | Every metric of every `ExperimentRun`, per run, with the direction-aware best value |
| `figures` | The experiment's VLM-checked figures (`exp_fig_N`, with captions) |
| `citations` | The topic's knowledge base: compiled/included library papers plus shelf papers, each with a stable bibkey |

This is the *only* material the drafting agent is allowed to state facts from — see the guardrails
below.

### AI drafting

Click **AI draft** to start a drafting task (kind `paper_writing`; one at a time per manuscript):

1. **Initialize structure** (step 1 in the dialog, and automatic if you skip it): the document body
   of your main file is replaced with a section skeleton (`% POLARIS_SECTION: ...` markers) in a new
   `draft.tex`, which becomes the compile main file. Your original file is kept untouched.
2. **Pick sections** — all of them, or a subset — and add optional notes for the AI.
3. The agent drafts **section by section** in a fixed order: Introduction → Method → Experimental
   Setup → Results → Conclusion → Abstract, then compiles, then writes **Related Work** last, then
   runs the final compile. The run only completes if the final compile succeeds.

While it writes you can watch it **live in the editor**: the text streams into the section as the
AI "types", with a status cursor showing the phase (typing / revising / compiling). Collaborator
edits elsewhere in the file merge normally through the CRDT.

Every section passes three **hard static checks** before it is accepted:

- `\cite{key}` may only use bibkeys present in the fact pack's citations — a citation can never be
  invented;
- `\includegraphics` may only reference fact-pack figure ids (as `figures/<fig_id>.pdf`);
- every percentage and decimal in the text must match a real `ExperimentRun` metric value (±0.01),
  with small integers, years, and section/table numbers exempted.

A violating draft is rewritten (up to twice, with the violations quoted back). If it still fails,
the section is written anyway but **prefixed with a `% TODO` comment listing the problems**, and a
"needs review" activity is recorded — the run degrades loudly instead of silently shipping an
unverifiable claim. Clean sections get one self-reflection polish pass (which must itself re-pass
the checks, otherwise the original stands).

**Related Work** is special: the candidate pool is the fact-pack citations plus the top-10
Semantic Scholar title matches for your paper, and the agent may cite *only* from that pool. Any S2
candidate it actually cites is added to the fact pack (and `references.bib`) as a `@misc` entry, so
the compile never hits an undefined citation.

### Bibliography, one click

**Refresh references** regenerates `references.bib` from the current fact pack — stable citation
keys of the form `{firstAuthor}{year}{firstWord}` — and fixes the `\bibliography` wiring in the main
file if it is missing or points elsewhere. Run it after the knowledge base or the experiment
changed; drafting runs do it automatically.

### Inline AI assist

For hand-editing, select text in the editor and open the assist panel:

- **Polish** — improve the selected passage;
- **Rewrite** — transform it following your instruction ("more concise", "active voice", …);
- **Continue** — extend from the cursor.

The result streams in beside the editor and is only applied when you accept it. The same fact-pack
constraints are given to the model, and the applied text is statically checked afterwards — issues
are shown as warnings rather than blocking, since you are in the loop.

<!-- screenshot: assist panel over a selected paragraph with polish/rewrite/continue modes -->

### arXiv export and submission

- **Export arXiv** builds a clean, submission-ready `tar.gz`: sources, `references.bib`, figures,
  and a freshly generated `.bbl` (the server recompiles once with intermediates kept so the `.bbl`
  matches the current sources), with build artifacts stripped. Any caveat — e.g. no compiler
  available to produce the `.bbl` — is reported as an export note.
- **Submit** requests submission approval. It requires the latest compile to be `ok` **and** the
  manuscript to have passed [paper review](paper-review.md); it then opens a `paper_submission`
  approval gate. Approval marks the manuscript **submitted**; rejection returns it to `compiled`.

## Step-by-step usage

1. Open **Paper Writer**, pick your topic, click **New manuscript**, choose a title and template.
2. Optionally link the idea and experiment when creating it — that is what populates the fact pack
   with hypotheses, metrics, and figures.
3. Open the manuscript, click **AI draft**, initialize the structure, select sections, start.
4. Watch the draft stream in; fix any `% TODO(AI …)` blocks it left for you.
5. Edit by hand with collaborators; use select-text assist for polish; **⌘S** to compile as you go.
6. **Refresh references** if the library changed; check the **Fact pack** drawer when in doubt about
   what the AI is allowed to claim.
7. Run [paper review](paper-review.md), fix what it finds, then **Submit** (or **Export arXiv**).

## Key settings

- **Main file** and **compiler** — top bar dropdowns; per manuscript.
- **Collaborators** — manage who can edit and get a share link from the **Collaborators** dialog.
- **Templates** — admins and users can upload zip templates; built-in style files stay read-only.
- **Pin / trash** — manuscripts can be pinned to the top of the list; deletion is a two-step
  trash + permanent purge, restorable in between.

## Tips and limits

- Compilation has a hard timeout; a paper that compiles for minutes usually has a runaway package
  or figure problem — check the diagnostics panel and trim.
- The drafting agent writes into `POLARIS_SECTION` markers. If you delete the markers, section
  drafting falls back to appending at the end of the document — keep them where you want the AI to
  write.
- Numbers you type by hand are not blocked by the writer — but they will be caught by
  [paper review](paper-review.md)'s fact check if they do not match the experiment record.
- Version history: every AI edit and structural change snapshots the file; use the history dialog on
  a file to inspect and restore earlier versions.
- One drafting task per manuscript at a time; the top bar shows "AI writing — view task progress"
  linking to the [task console](task-system.md) while it runs.
