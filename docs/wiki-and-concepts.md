# Wikis and Concepts

Every paper in Polaris can have a **wiki**: a long, illustrated read-through written by an LLM from
the paper's full text and figures. Inside that prose, key terms are marked with `[[double
brackets]]`. Those marks are what produce the **concept** entries — the small dictionary of methods,
architectures, problems, metrics and datasets that the lab accumulates as it reads.

Two facts drive nearly every design decision in this document:

- **A wiki belongs to a paper, not to a library.** One paper, one wiki, platform-wide
  (`paper_wikis`). Whoever compiles it, everyone sees the same text.
- **A concept belongs to the platform, not to a library.** One row per concept, `slug` unique
  globally, no `library_id`. "Which concepts does library X have" is *derived* from that library's
  papers, never stored.

Both were true only recently. Wikis used to live in four places (the library membership row, the
daily-feed entry, the personal-library entry, and a shelf snapshot) and concepts used to be
library-scoped with a `NOT NULL library_id`. Section 3 explains what the old columns are still doing
in the schema.

Where things live:

- Compile: `app/services/wiki_compile.py`, figure selection in `app/services/figure_annotate.py`
- Concepts: `app/services/concepts.py`
- The single read/write door for a wiki row: `app/services/paper_wiki.py`
- Tables: `app/models/paper.py` (`PaperWiki`, `Concept`, `paper_concepts`)
- HTTP: `app/api/papers.py` (recompile), `app/api/daily.py` (daily compile),
  `app/api/concepts.py`, `app/api/libraries.py` (library-scoped concept list + relink)
- Inside a library-build task: `app/agents/voyage/actions_wiki.py` (`wiki.compile`,
  `wiki.link_concepts`)
- UI: `src/frontend/src/features/wiki/`, `src/frontend/src/lib/markdown.tsx`

Paths above are relative to `src/backend/` unless stated otherwise. For where compiling sits in the
larger paper lifecycle, see [Literature Management](literature-management.md#paper-lifecycle); for
how a library-build run is driven, see [The Task System](task-system.md#51-building-a-library--wiki_ingest).

---

## 1. How a wiki gets written

### 1.1 The three trigger paths

All three end in the same function, `wiki_compile.compile_paper()`
(`app/services/wiki_compile.py:143`), and all three write the same row through
`paper_wiki.upsert_wiki()` (`app/services/paper_wiki.py:20`).

| Path | Entry point | Runs for | Concept linking |
| --- | --- | --- | --- |
| **Batch, inside a library build** | step `wiki.compile` of a `wiki_bootstrap` / `wiki_ingest` run (`app/agents/voyage/actions_wiki.py:788`) | every membership in the library with `status == "fetched"`, ordered by relevance, capped by the run's knobs | yes — the separate `wiki.link_concepts` step that follows |
| **One paper, by hand** | `POST /papers/{paper_id}/recompile` → `wiki_compile.recompile_paper()` (`app/api/papers.py:585`, `app/services/wiki_compile.py:210`) | that one paper | yes — inline, `link_paper_concepts()` at the end |
| **One paper, from the daily feed** | `POST /daily/papers/{entry_id}/compile` → `daily_feed.compile_entry_wiki()` (`app/api/daily.py:371`, `app/services/daily_feed.py:780`) | that one paper | **no** — see below |

The daily path deliberately has no library in hand: a feed paper usually belongs to no library at
all. `link_paper_concepts()` takes a `LibraryPaper` membership (for LLM cost attribution), so it
cannot be called there. The consequence is real and worth knowing: **a paper compiled only from the
daily feed has a wiki full of `[[links]]` but no `paper_concepts` rows**, so those links resolve to
"not in the knowledge base yet" until the paper lands in a library and a relink runs. See section 6.

The batch path is the only one that is *idempotent by status*: it selects `status == "fetched"` and
sets `compiled` when done, so a resumed run never recompiles what it already compiled. The two
manual paths always recompile.

### 1.2 What goes into the compile call

`build_compile_prompt()` (`app/services/wiki_compile.py:106`) assembles one user message:

- **Body.** The extracted full text if `paper.full_text_path` exists on disk, otherwise the
  abstract, otherwise the literal string `（无正文）`. Either way it is truncated to
  `FULLTEXT_PROMPT_CHARS = 24000` characters (`wiki_compile.py:37`). The prompt states which source
  was used (`正文来源: full_text | abstract`) so the model knows how much detail it can rely on.
- **Metadata.** Title, author names, year, venue.
- **Figures.** `important_figures_with_bytes()` (`app/services/figure_annotate.py:180`) reads the
  PNGs of every figure flagged `important` in `paper.figures`, downsamples anything too big for the
  vision model, and returns at most **6** of them. Those bytes ride along as images on the LLM call;
  a matching text block lists them as `fig:N【kind, suggested section】— caption` plus explicit
  instructions to interleave them (`_figure_prompt_section`, `wiki_compile.py:88`).

Figures only get flagged as important because something ran `annotate_figures()`
(`figure_annotate.py:212`) first — a `librarian`-stage multimodal call that picks the 2–6 figures
that matter and captions them. Both the batch step and the two manual paths do this immediately
before compiling if the paper's figures have not been annotated yet. The manual paths go further:
if the paper has never had figures extracted, *or* the last annotation pass flagged nothing as
important, they re-extract candidates from the PDF first
(`wiki_compile.py:225-241`, `daily_feed.py:823-832`).

**How `![[fig:N]]` lines up with the images.** `N` is the `index` field in the `paper.figures` JSON
list, which is also the path key on disk (`<data_dir>/papers/<paper_id>/figures/fig_<N>.png`) and
the key in `GET /papers/{id}/figures/{index}/image`. The model is told to emit `![[fig:N]]` on its
own line using only the numbers from the list it was given. Before anything is stored,
`strip_invalid_figure_markers()` (`wiki_compile.py:67`) walks the output line by line and **deletes
any whole line containing a marker whose index is not in `paper.figures`** — a hallucinated figure
reference costs that line, not a broken image in the reader. The frontend renders standalone
`![[fig:N]]` lines as embedded images and silently drops inline ones
(`src/frontend/src/lib/markdown.tsx`).

### 1.3 Compiling carries no library context — on purpose

`compile_paper()` never sees the library's research statement, its relevance rubric, or its
keywords. The docstring says it plainly (`wiki_compile.py:109`): *only the paper itself goes in.*
`library_id` and `project_id` are passed to the LLM router purely so the call is billed to the right
budget.

The reason is the whole point of the unification. The same paper sitting in three libraries used to
get three compiles producing three slightly different articles, at triple the cost, and a reader
moving between libraries would see the text change under them for no reason a reader could
perceive. Once you accept that the output is a *general* read-through of a paper — not an argument
about how the paper relates to one direction — there is nothing left for the library to contribute,
and one row per paper follows.

**The one external injection point that survives** is the skill system. The batch step calls
`ctx.skill_guidance("wiki.compile")` (`actions_wiki.py:813`) and passes the result as
`extra_guidance`, which is appended to the Librarian system prompt (`wiki_compile.py:185`). Skills
enabled on that target contribute writing-style rules — the built-in one,
`librarian-note-style` (`app/services/builtin_skills.py:31`), says things like "write the mechanism,
not a restatement of the pipeline" and "takeaways must be concrete enough to act on". This is
*guidance about how to write*, not *context about a direction*, which is why it is allowed through.

Note the asymmetry: `extra_guidance` defaults to `""`, and only the batch path fills it. A manual
recompile and a daily compile use the bare system prompt. So the same paper can be written slightly
differently depending on which button produced it.

The prose contract itself is fixed in `LIBRARIAN_SYSTEM_PROMPT` (`wiki_compile.py:42`): Chinese
markdown, a flowing multi-paragraph article rather than a bullet outline, a five-heading skeleton
(TL;DR / background and motivation / method / experiments and results / discussion and takeaways),
LaTeX for maths, and — the part that matters for section 2 — **mark key concepts with `[[name]]` at
first mention, inline, and never append a "related concepts" list at the end**.

### 1.4 Where the output lands

`upsert_wiki()` (`app/services/paper_wiki.py:20`) is the only writer. It looks up
`paper_wikis` by `paper_id` and either inserts or overwrites the whole row:

- `content` — the validated markdown.
- `model` — taken from `CompletionResult.model`, i.e. **what the provider actually answered with**,
  not what the routing table says it should have used. If an admin edits the routing mid-run, the
  record still reflects reality.
- `compiled_by` — the person, always overwritten. The batch path records `ctx.run.created_by` (the
  human who started the sync — public-library runs bill to the system, but a human still pressed the
  button); the manual paths record the caller.
- `updated_at` is bumped even when the text is byte-identical, so "last compiled" always means "last
  time someone paid for a compile".

`upsert_wiki` also calls `set_committed_value(paper, "wiki", wiki)` so the in-memory `Paper` picks up
the new row — everything downstream (`paper.wiki_content`, the concept linker, the response schema)
reads through the relationship, and without this the next read would fire a lazy load and blow up
under async SQLAlchemy. The same trick is used when creating a pool paper (`models/paper.py:115`).

Read paths never branch. `Paper.wiki` is `lazy="selectin"`, so lists and details get the wiki for
free; `PaperView.compiled_at / compiled_model / wiki_content` (`app/services/papers.py:128-142`)
just project the relationship. The daily feed entry detail reads the very same row
(`daily_feed.py:663-668`) and the personal library reads it by `paper_id`
(`paper_wiki.content_for()`, called from `app/api/library.py:277`). That is what makes "compile it
in the library, see it in the feed" work with no synchronisation at all.

### 1.5 Recompiling: who, and what it costs you

Recompiling is not privileged. Any user with full LLM access (`require_llm_task`) who can see the
paper can do it — for `POST /papers/{id}/recompile` that means the paper is in a library they can
reach (`_get_member_paper` without the pool fallback, `app/api/papers.py:113`); for the daily
endpoint, being logged in is enough, because the feed is lab-wide.

The semantics are blunt: **overwrite, no history.** There is no version table, no diff, no undo. The
old text is gone the moment the new one is flushed, and `compiled_by` becomes the new person's.

The only guard is a confirmation dialog, and it is built from data the API deliberately ships for
this purpose. `PaperDetail` carries `compiled_by_name`, resolved by `paper_wiki.compiler_names()`
(`app/services/paper_wiki.py:73`) and attached in `app/api/papers.py:87`; the daily item does the
same in `daily_feed.py:669`. The frontend renders "the current wiki was compiled by X at T with
model M — recompiling overwrites it and the old one cannot be recovered"
(`src/frontend/src/features/wiki/PapersTab.tsx:1043`,
`src/frontend/src/features/daily/DailyPage.tsx:541`). `compiled_by` is `ON DELETE SET NULL`, and
rows created by the backfill migration have it empty, so "an unknown user" is a normal thing to see.

The daily endpoint additionally refuses to start a second compile of the same entry while one is in
flight (`_COMPILING`, a plain in-process set at `daily_feed.py:759`) and answers `409
COMPILE_IN_PROGRESS`. There is no such guard on `/papers/{id}/recompile`, and none of this is
cross-process; see section 6.

### 1.6 What happens when things fail

The pipeline is built so that a partial failure degrades the output instead of losing it.

- **No PDF.** Compile runs on the abstract and produces a text-only article. The daily path tries a
  best-effort `fetch_pdf()` first and swallows every failure (`daily_feed.py:806-822`); the manual
  recompile just skips the figure work when `pdf_path` is missing.
- **Figure annotation fails.** `annotate_figures()` retries the LLM once, then degrades
  deterministically: the four largest figures by area get `important=true` with no captions
  (`DEGRADE_TOP_N`, `figure_annotate.py:24`). In the batch step, even a total failure of this call
  is only logged — compile proceeds (`actions_wiki.py:845-848`).
- **The model ignores the figures.** If images were sent but the output contains no `![[fig:N]]`
  marker at all, `compile_paper` retries once with a blunter instruction; if that also fails it
  accepts the text-only draft (`wiki_compile.py:174-206`). The figures are still browsable in the
  gallery either way.
- **Empty response.** `ValueError("librarian returned empty content")` — the endpoints map any
  exception here to `502 COMPILE_FAILED`, and nothing is written.
- **One paper of a batch fails.** Each paper is compiled in its own session with its own commit and
  its own `try`; the failure is collected into the step observation's `failed` list and the run
  continues (`actions_wiki.py:881-892`). `CancelledError` is re-raised rather than swallowed, so a
  killed worker resumes at the checkpoint instead of marking papers as failed.
- **Affiliation extraction rides along.** When the admin setting `affiliation_extraction_mode` is
  `on_compile` and the paper has no affiliations yet, the compile prompt asks for an author↔institution
  block after the article. The block is **stripped from the body whether or not it parses**
  (`parse_and_strip_affiliation_block`, called at `wiki_compile.py:199`) — a parse failure costs the
  affiliations, never the wiki.

---

## 2. How concepts get made

### 2.1 The names are not an LLM answer — they are a regex over the prose

This is the part that is easy to get wrong when reading the code from the outside. There is **no
prompt anywhere that asks a model for "the list of concepts in this paper."** What happens is:

1. The Librarian, while writing the article, marks terms inline as `[[name]]` because the system
   prompt told it to (section 1.3). The prompt caps this at 5–8 marks per article and asks for
   *cross-paper* terms only — a benchmark, dataset or model codename the paper itself coined is not
   to be marked unless it is already standard vocabulary.
2. `extract_wikilinks()` (`app/services/concepts.py`) runs `WIKILINK_RE` over the stored markdown
   and returns the de-duplicated, order-preserving list of names. The regex accepts `[[name]]`,
   `[[name|alias]]` and `[[name#anchor]]`, keeping only the name part, and skips any match preceded
   by `!` (whitespace tolerated) so that `![[fig:N]]` embeds are not mistaken for concepts.
   Extraction judges nothing else: whether a name deserves an entry is a judgement call, and it is
   made later (2.1.1).
3. Each name is looked up in `concepts` **by exact name, platform-wide**. If a row exists — because
   any other paper in any other library already produced that term — it is reused as is. Only names
   with no row are new.
4. New names are inserted as `status = "candidate"` — no LLM call at all, not even for a definition.

So the vocabulary is proposed by the writing model as a side effect of writing, and gated afterwards.

### 2.1.1 The gate: two papers, then a verdict

Marking is cheap for the model and 89% of the entries it produced were cited by exactly one paper —
almost all of them names that paper had coined. Two filters run *after* extraction, and neither one
deletes anything:

- **Threshold (deterministic).** A concept stays `candidate` until `paper_concepts` shows
  `CONCEPT_PROMOTION_MIN_PAPERS = 2` distinct papers. Candidates are linked normally but are
  invisible everywhere a user could see a concept (list, graph, search, export, lab stats,
  `[[wikilink]]` resolution, concept detail → 404). A `[[link]]` to a candidate reads as "not in the
  knowledge base yet", which is exactly what it is — the second citing paper makes it appear, with
  no human step.
- **Validity (a judgement, so: the model).** Reaching the threshold triggers
  `promote_ready_concepts()`, which asks the `extract` stage — in the same call that writes the
  definition — whether each name is a meaningful academic concept at all. `fig:1`, `Figure 2`, bare
  numbers, sentence fragments come back `valid: false` and land in `status = "rejected"`, a terminal
  state that is never re-judged. This is what catches the junk the threshold cannot: `fig:1` is
  mis-marked by dozens of papers, so it clears "2 papers" easily.

Because the definition call and the validity call are the same call, the gate is close to free: only
concepts that already cleared the threshold ever cost a token, versus one definition per name before.
If the verdict cannot be obtained (no LLM configured, call failure, name missing from the response)
the concept is **promoted anyway** with a placeholder definition and `validated_at` left null, so a
timeout can never silently kill a real concept; the next sync re-judges it.

A term's definition is still written once, by whoever's compile pushed it over the threshold, and is
then shared by every later paper that uses it.

### 2.2 Definitions: batched, on the small model

`fetch_concept_definitions()` (`concepts.py:202`) asks for definitions in batches of
`_DEF_BATCH_SIZE = 40` names (`concepts.py:50`). The batching is not about throughput — it is about
truncation. One call carrying hundreds of names produces a response that hits `max_tokens`
mid-JSON, the parse fails, and *the entire batch* falls back to placeholders. Forty at a time keeps
each response comfortably inside the window.

The call goes to the **`extract`** stage, not `librarian` (`concepts.py:251`). The two roles were
split deliberately and the split is documented at `app/core/llm/router.py:41-43`: `librarian` is for
long-form and multimodal work (wiki compiling, figure selection, slide generation) and wants a
strong model; `extract` is for short structured JSON (author↔affiliation parsing, concept
definitions, the library-setup wizard) where a small model is enough. Getting this backwards is an
easy and expensive mistake, so: **compiling is `librarian`, defining is `extract`.**

The prompt (`CONCEPT_DEF_SYSTEM_PROMPT`) asks for a single JSON object
`{"concepts": [{name, valid, definition, category}]}` — the verdict of 2.1.1 rides along with the
definition. The response is parsed by slicing from the first `{` to the last `}`, which tolerates
fenced code blocks and stray prose. Categories are clamped to the fixed set
`method | architecture | methodology | problem | metric | dataset | other` by
`normalize_category()` — anything unrecognised becomes `other`.

**Retry.** A batch that returns nothing usable (call error, rate limit, unparseable JSON) is retried
**exactly once**, with the same input (`concepts.py:224-232`). Under load this recovers most
batches. Anything a name does not come back with after that retry is handled below. Failure is
never allowed to propagate: `_fetch_definitions_batch` catches everything except `CancelledError`
and returns an empty dict (`concepts.py:269-273`).

### 2.3 Placeholders, and how they heal

A concept whose definition never arrived is still created — the entry, the slug and the links matter
more than the sentence. Its definition becomes `placeholder_definition(name)`, literally the name
followed by `（定义待补充）` (`concepts.py:56`), and `is_placeholder_definition()` detects it later by
that suffix.

Backfilling happens in `review_active_concepts()`, called at the end of the whole-library pass
`link_all_paper_concepts()`:

- It gathers the **active** concepts in scope that need a second look — those already linked to a
  paper of *this* library, plus any concept whose name appears in this round's wikilinks (i.e. is
  about to be linked). "Needs a second look" means either a placeholder definition, or
  `validated_at IS NULL` (never judged — which is how every entry that predates the gate looks, and
  where the `fig:1`-style junk hides). Other libraries' entries are left for their own syncs; a
  nightly library sync should not redefine the whole platform. An entry judged invalid here is
  moved to `rejected` and disappears from the library, without deleting a row.
- **Manual relink** (`backfill=True`) retries all of them.
- **The automatic step inside a task** (`backfill=False`) retries only the `_AUTO_BACKFILL_CAP = 60`
  oldest (`concepts.py:52`, `concepts.py:397`). This is the compromise: an occasional failed batch
  heals within a day or two of normal syncing, but a library sitting on hundreds of placeholders
  does not re-pay for hundreds of definition calls every single night.
- New names and retried placeholders are requested in the same call set, and a returned definition
  is only accepted if it is not itself a placeholder string (`concepts.py:427-431`).

### 2.4 Relink: the repair button

"Relink" is `link_all_paper_concepts()` exposed directly, and it is what the automatic
`wiki.link_concepts` step calls too (`actions_wiki.py:919`). Two endpoints reach it:

- `POST /libraries/{library_id}/concepts/relink` — the library-scoped one, for anyone who can manage
  the library (`app/api/libraries.py:874`).
- `POST /projects/{project_id}/concepts/relink` — the topic-scoped one, which resolves the topic to
  its origin library and runs there, returning zeros if the topic has no resolvable library
  (`app/api/concepts.py:87`).

Both pass `backfill=True`. What the pass actually does, in order:

1. Select every paper of the library whose membership status is `compiled` or `included` **and**
   which has a `paper_wikis` row (an inner join — no wiki, nothing to extract)
   (`concepts.py:351-361`).
2. Extract wikilinks from each, union the names, look them all up by name, create the missing ones
   as candidates (no definition, no LLM call).
3. Insert the missing `(paper_id, concept_id)` pairs. Existing pairs are skipped, so it is fully
   idempotent and safe to run repeatedly.
4. **Prune stale links.** For each paper whose wiki text is non-empty, delete the links to concepts
   the current text no longer mentions. A paper with an empty or missing wiki is skipped entirely,
   so a failed compile can never silently strip a paper's concepts.
5. **Sweep orphans** — see below.
6. **Promote and review** — candidates that reached two papers are judged and promoted (2.1.1), then
   active entries needing a second look are re-judged (2.3). Both happen after the link work has been
   committed, so no LLM call is made while a write transaction is open.

You need it when a wiki exists but the linking never ran: historical data, papers compiled from the
daily feed, papers whose compile succeeded in a run that died before `wiki.link_concepts`, or an
older definition batch that left placeholders you now want filled.

The single-paper equivalent, `link_paper_concepts()`, runs automatically after a manual recompile and
does the same thing for one paper: extract, reuse-or-create as candidates, link, remove the links the
new text dropped, delete any concept that was left with zero references, then promote whichever of
this paper's concepts just reached two papers. It also
returns immediately when `paper.wiki_content` is falsy — same anti-footgun as step 4.

### 2.5 Orphans

A concept is an orphan when **no paper anywhere references it** — `paper_concepts` has no row for
it. The check is platform-wide by definition, because the concept is platform-wide
(`delete_orphan_concepts()`, `concepts.py:276`).

Two things about the standard deliberately:

- **Trashed papers still count as references.** Trashing a library paper flips the membership to
  `excluded`; the membership and the `paper_concepts` rows survive, so the concept survives too, and
  recalling the paper restores everything. There is a test pinning this
  (`tests/test_concepts_relink.py::test_relink_keeps_concepts_referenced_by_trash_papers`).
- **A rejected concept is not an orphan.** Its `paper_concepts` rows stay, so it is never collected;
  it simply stays invisible for good. Nothing about this gate deletes data.
- **Scope differs by caller.** `link_paper_concepts()` passes `candidate_ids` — only the concepts it
  just unlinked from this paper are considered. `link_all_paper_concepts()` passes nothing, which
  means a single library's relink runs an **unscoped, whole-table** orphan sweep. That is how orphans
  left behind by paper deletion elsewhere eventually get collected, and also a cross-library side
  effect worth knowing about (section 6).

Deleting a paper does not clean concepts at the time of deletion. `gc_orphan_papers()`
(`app/services/papers.py:745`) removes the pool row when nothing references the paper any more, and
the `paper_concepts` rows go with it by `ON DELETE CASCADE` — but the now-childless `Concept` row
stays until the next relink sweeps it.

---

## 3. The data model

Three tables, all in `app/models/paper.py`.

### `paper_wikis` — `PaperWiki` (`models/paper.py:90`)

| Column | Notes |
| --- | --- |
| `paper_id` | FK → `papers.id`, `ON DELETE CASCADE`, **unique** (`uq_paper_wikis_paper`) — the "one per paper" rule is a database constraint, not a convention |
| `content` | the markdown, `NOT NULL` |
| `model` | model name as reported by the provider; null for migrated rows |
| `compiled_by` | FK → `users.id`, `ON DELETE SET NULL`; null for migrated rows and deleted users |
| `created_at` / `updated_at` | `updated_at` is the "last compiled" timestamp surfaced as `compiled_at` |

Related by `Paper.wiki` (`uselist=False`, `lazy="selectin"`, `cascade="all, delete-orphan"`).

### `concepts` — `Concept` (`models/paper.py:259`)

| Column | Notes |
| --- | --- |
| `name` | indexed, not unique — uniqueness is enforced on the slug |
| `slug` | **globally unique** (`uq_concepts_slug`) |
| `definition` | the one-sentence LLM definition, or a placeholder; **null while the entry is still a candidate** — definitions are only written at promotion (2.1.1) |
| `category` | one of the seven fixed values, or null |
| `status` | `candidate` (default — fewer than two citing papers, invisible) / `active` (in the concept library) / `rejected` (judged not to be a concept; terminal). Every read path that a user can reach filters on `active` |
| `validated_at` | when the model last confirmed the name is a real concept. Null means never judged — the state every entry that predates the gate was left in by the migration, and the trigger for a re-judge on the next sync |
| `wiki_content` | a long-form markdown body for the concept itself. **Nothing writes it.** It is read by the concept detail response and by the Obsidian export (`app/services/wiki_export.py:235`), so it renders if it ever gets populated, but no code path produces it today |

There is no `library_id`. Slugs come from `wiki_slug()` (`concepts.py:82`): lowercase, keep word
characters and CJK, collapse everything else to `-`, fall back to a hash prefix if nothing survives.
`_free_slug()` (`concepts.py:88`) checks for a collision and appends six random hex characters if
needed — so two different names that fold to the same slug coexist rather than one losing.

### `paper_concepts` (`models/paper.py:39`)

A plain association table: `(paper_id, concept_id)`, composite primary key, both sides `ON DELETE
CASCADE`. No timestamps, no provenance, no per-link metadata — a link means "this paper's wiki text
mentions this concept", and that is all it means.

### Why there is no `library_concepts` table

"Which concepts does this library have" is a **derived query**, `library_concept_ids()`
(`concepts.py:104`): `paper_concepts ⋈ library_papers WHERE library_id IN (...)`. Every surface that
needs it goes through that subquery — the library concept list (`app/api/libraries.py:599`), the
topic concept list (`app/api/concepts.py:69`), the concept keyword search
(`app/services/papers.py:1108`), and the library concept counter
(`app/services/libraries.py:324-335`, which is a `COUNT(DISTINCT concept_id)` over the join).

The reason for deriving rather than storing is that the alternative has no consistent update rule.
Membership in a library changes for reasons that have nothing to do with concepts — a paper is
added, removed, trashed, recalled, or merged into a duplicate — and each of those would have to
maintain a link table correctly, including deciding whether removing the last paper that mentions a
term should remove the term from the library. Derivation gets all of that right for free and can
never drift. The cost is a join on every concept listing, which is fine at lab scale (section 6).

### The retired columns

The unification migration (`alembic/versions/a7d0c9e51b34_paper_wikis_unified.py`) copied existing
wikis into `paper_wikis` and **deliberately kept every source column**, so it could be rolled back
by dropping one table. They are still on the models, marked as retired, and **no code reads or
writes them**:

| Table | Retired columns | Model |
| --- | --- | --- |
| `library_papers` | `wiki_content`, `compiled_at`, `compiled_model` | `models/library_direction.py:100-110` |
| `daily_feed_entries` | `wiki_content`, `wiki_model` | `models/daily_feed.py:44-47` |
| `user_library_entries` | `wiki_content` | `models/library.py:40-42` |
| `topic_papers` | `wiki_snapshot`, `snapshot_at` | `models/topic_shelf.py:42-45` |

Backfill priority when merging them was: the newest library row by `compiled_at`, else the daily
entry. `compiled_by` was unknowable, hence all-null for pre-migration wikis. Dropping the columns is
a separate migration, held until the new shape has proven itself in production.

The concept migration (`b6c2f81d4a09_concepts_paper_level.py`) is the same philosophy with more
work: same-named concepts across libraries were merged into one row — **keeping the one with the
longest definition**, on the theory that a longer AI-written definition usually covers the term's
boundaries and uses, whereas the most recent one is only an accident of compile order — links were
repointed (or dropped when the target already had that paper), remaining slug collisions were given
random suffixes, and only then was `library_id` dropped and `uq_concepts_library_slug` replaced by
`uq_concepts_slug`. Everything removed was archived in `concepts_pre_unify` and
`paper_concepts_pre_unify`, and `downgrade()` fully reconstructs the old world from them.
`tests/test_migrations_wiki_unify.py` runs the round trip.

---

## 4. Scope: what a concept looks like from where you clicked

The entry itself — name, definition, category, slug — is the same everywhere. **The only thing that
varies with context is the list of papers shown on it.**

### Two contexts

**Library context** (the library workspace's Concepts tab, the graph, the library chat). The concept
list comes from `GET /libraries/{id}/concepts` or `GET /projects/{id}/concepts`, both derived from
that library's (or that topic's associated libraries') papers. Clicking a concept opens it with
`?library=<id>`, and the related-papers list is filtered to that library.

**Pool context** (the daily feed, the personal library, the reader, the shelf). There is no library
to speak of, so nothing is filtered: the concept opens platform-wide and lists every paper linked to
it. `usePoolConceptNav()` in `src/frontend/src/features/wiki/shared.tsx:57` is the single place that
implements this.

### One endpoint, one optional parameter

`GET /concepts/{concept_id}` (`app/api/concepts.py:117`) takes `library_id` as an **optional** query
parameter, and that single parameter is the whole difference:

- **Given** — the library is checked for visibility, the related-papers list is restricted to it,
  and `library_id` / `project_id` come back in the response so the UI knows where "back" goes.
- **Omitted** — the papers list is platform-wide. The response still tries to supply a landing
  library for navigation, by finding the earliest `library_papers` row among the papers that use the
  concept (`app/api/concepts.py:145-155`). If the concept only hangs off pool papers, that comes
  back null, and the page opens perfectly well without it.

Note that the two contexts also disagree slightly about `paper_count`: the list endpoints report the
platform-wide reference count for every concept (`list_concepts()`, `concepts.py:126`), while the
detail endpoint with a `library_id` reports the count *within that library* (`api/concepts.py:160`).
Clicking a concept in a library's list can therefore show a smaller number than the list did.

### Resolving `[[double brackets]]`

`src/frontend/src/lib/markdown.tsx` renders every `[[name]]` as a clickable chip and hands the raw
name to a callback. What the callback does depends on the context:

- **In a library** (`LibraryBrowse.tsx:887`, `WikiPage.tsx:152`): call the library/topic concept list
  with `q=name`, which is a substring `ILIKE` **restricted to that library's derived concepts**, then
  pick the exact case-insensitive name match, or fall back to the first row. Miss → a toast, "not in
  the library yet".
- **In the pool** (`shared.tsx:63`): call `GET /concepts?name=` — `find_by_name()`
  (`concepts.py:141`) matches on exact lowercased name, or, failing that, on the slug, which absorbs
  spacing and hyphenation differences. Because concepts are globally unique this returns at most one
  row. Miss → a toast, "not in the knowledge base yet".

Both misses are informational, not errors. An unresolved wikilink is a normal state: the Librarian
marks up a term the moment it writes about it, and the concept row only appears after the linking
step runs.

### Concepts with no library at all

Nothing requires a concept to belong to a library, and the API handles the case explicitly
(`tests/test_paper_wiki_unified.py::test_pool_only_paper_concepts_open_without_library`). In
practice it arises when a paper that had concepts is removed from every library while still being
referenced elsewhere — a daily-feed entry, a shelf, someone's personal library. The membership rows
go away, the `paper_concepts` rows do not (they hang off `papers`, not `library_papers`), and the
concept is now reachable only from pool surfaces. It is not an orphan and will not be swept, because
a paper still references it.

---

## 5. One paper, end to end

Take an arXiv paper that a library sync picks up. Each step names the table it touches.

1. **Into the pool.** `wiki.search_candidates` creates the `papers` row (deduped) and a
   `library_papers` row with `status='candidate'`.
   → `papers`, `library_papers`
2. **Scored.** `wiki.score_relevance` writes `relevance_score` and moves the membership to `scored`
   (or `excluded`).
   → `library_papers`
3. **PDF and full text.** `wiki.fetch_extract` downloads the PDF, extracts the text, chunks it, and
   pulls up to 12 figure candidates into `papers.figures` with `important=false`, `caption=null`.
   Membership → `fetched`.
   → `papers` (`pdf_path`, `full_text_path`, `figures`), `paper_chunks`
4. **Figures judged.** At the start of `wiki.compile`, `annotate_figures()` sends the candidate
   images to the `librarian` stage; 2–6 come back flagged `important` with a `kind` and a Chinese
   caption. On failure, the four largest are flagged instead.
   → `papers.figures`
5. **Compiled.** `compile_paper()` sends title/authors/full text (≤24k chars) plus up to 6 important
   figure images and their caption list, with the project's `wiki.compile` skills appended to the
   system prompt. The output is scrubbed of invalid `![[fig:N]]` markers and upserted.
   → **`paper_wikis`** (one row: content, model, `compiled_by`), `library_papers.status='compiled'`
6. **Concepts extracted.** `wiki.link_concepts` runs `link_all_paper_concepts()` over the library.
   `extract_wikilinks()` pulls, say, `[[对比学习]]`, `[[InfoNCE]]`, `[[线性探测]]` out of the article.
   `[[对比学习]]` already exists from another library's paper → reused untouched. The other two are
   new.
   → nothing written yet
7. **Definitions.** The two new names go into one `extract`-stage batch (batch size 40). The JSON
   comes back with a sentence and a category each → two `concepts` rows, `slug` globally unique. Had
   the call failed twice, they would have been created with `「InfoNCE（定义待补充）」` and picked up by
   a later relink.
   → **`concepts`**
8. **Linked.** Three `(paper_id, concept_id)` pairs inserted; existing pairs skipped. Then stale
   links for every paper in the library are pruned, and a platform-wide orphan sweep removes any
   concept nobody references.
   → **`paper_concepts`**
9. **Read in the library.** The Concepts tab lists concepts via `library_concept_ids()`. Clicking
   `[[InfoNCE]]` in the article searches the *library's* concepts for the name and opens
   `/concepts/<id>?library=<lib>`, whose paper list contains only this library's papers.
   → read-only
10. **Read in the daily feed.** The same paper's feed entry shows the same wiki text — it reads
    `paper_wikis` by `paper_id`, no snapshot involved — and its concept chips come from
    `Paper.concepts`. Clicking `[[InfoNCE]]` here goes through `GET /concepts?name=InfoNCE` and opens
    `/concepts/<id>` with no library filter: every paper platform-wide that mentions InfoNCE.
    → read-only
11. **Someone recompiles.** `POST /papers/{id}/recompile` re-annotates the figures, recompiles, and
    overwrites the single `paper_wikis` row — `compiled_by` becomes them. Then
    `link_paper_concepts()` re-extracts: the new text no longer mentions `[[线性探测]]`, so that link
    is deleted, and since no other paper references it, the concept row is deleted too. The change
    is instantly visible in the library, the feed, the shelf, and the personal library, because
    there is only one row.
    → `paper_wikis`, `paper_concepts`, `concepts`

---

## 6. Limits and sharp edges

**Concepts are only created from library papers.** `link_paper_concepts()` requires a `LibraryPaper`
and `link_all_paper_concepts()` is keyed by `library_id`. A paper compiled purely through the daily
feed gets a wiki and no concepts, and its `[[links]]` will report "not in the knowledge base yet"
until it joins a library and someone relinks. This is the most likely thing to surprise a user.

**A concept that exists platform-wide can be unresolvable inside a library.** In-library link
resolution searches only the library's derived concepts. If the term exists but no paper in *this*
library is linked to it, the click fails with "not in the library yet" even though
`/concepts?name=` would find it.

**In-library resolution can open the wrong entry.** The library lookup is a substring search that
falls back to `data[0]` when there is no exact name match
(`WikiPage.tsx:129-131`, `LibraryBrowse.tsx:906-907`). `[[attention]]` in a library that has "flash
attention" but not "attention" opens flash attention.

**Concurrent compiles silently overwrite each other.** `upsert_wiki` is last-writer-wins with no
optimistic locking. `/papers/{id}/recompile` has no in-flight guard at all, and the daily endpoint's
guard is a Python `set` in one process (`daily_feed.py:759`) — with multiple API replicas, two
people can compile the same paper simultaneously and one result vanishes. The comment on that line
says as much.

**A single library's relink sweeps orphans platform-wide.** `link_all_paper_concepts()` calls
`delete_orphan_concepts()` with no candidate set (`concepts.py:481`), i.e. `DELETE FROM concepts
WHERE NOT EXISTS (...)` over the whole table. It is correct — a zero-reference concept is dead
everywhere — but it means one library's maintenance action does global work, and its cost grows with
the platform rather than with the library.

**Relink and the batch step load the whole library into memory.** Step 1 of the pass selects every
compiled paper *with its full wiki markdown* and builds several dicts over it
(`concepts.py:351-364`). There is no pagination and no limit. For a few thousand papers at a few
kilobytes of markdown each this is tens of megabytes per call — acceptable, but not something to
scale another order of magnitude without changing.

**SQLite and PostgreSQL are not equivalent here.** Tests run on SQLite (`tests/conftest.py:15`),
production on PostgreSQL.
- `Concept.name.in_(all_names)` is a bind parameter per name. Older SQLite builds cap variables at
  999; a library with more distinct concept names than that would fail in tests while working in
  production (PostgreSQL's limit is 65535).
- `ILIKE` on PostgreSQL does Unicode-aware case folding; SQLAlchemy's `ilike` on SQLite becomes
  `LIKE`, which only folds ASCII. Irrelevant for CJK names, relevant for accented Latin ones.
- The concept migration uses `batch_alter_table` to drop `library_id`, because SQLite cannot drop a
  column in place.
- Semantic paper search is PostgreSQL-only (`papers.py:1131`), so `GET /projects/{id}/search`
  silently falls back to keyword mode elsewhere. Concept search is keyword-only on both.

**Truncation is invisible.** The body is cut at 24000 characters with no marker and no warning in the
response; a long paper is compiled from its first ~24k characters. Similarly, definition batches are
capped at 40 names and the automatic backfill at 60 placeholders per run — nothing tells the user
that the remainder was deferred.

**Invalid figure markers cost whole lines.** `strip_invalid_figure_markers` drops the entire line
containing a bad index. If the model writes prose and a marker on the same line, the prose goes too.

**Doc comments say "≤4 figures"; the code sends up to 6.** `wiki_compile.py:5` and
`actions_wiki.py:849` both say four, but `important_figures_with_bytes()` defaults to `limit=6` and
nothing overrides it. Four is the *degraded* count when figure annotation fails
(`DEGRADE_TOP_N`).

**`Concept.wiki_content` is dead weight.** Read by the API and the Obsidian export, written by
nothing. Either a long-form concept page is a feature someone still wants, or the column should go.

**No wiki history.** No versions, no diffs, no restore. The confirmation dialog is the entire safety
net, and it depends on `compiled_by_name`, which is null for every pre-migration row.

**`paper_concepts` carries no provenance.** You cannot tell from a link whether it came from a
Librarian markup, a relink of historical data, or a migration repoint — nor when. If link-level
auditing ever matters, the table needs columns.

---

## See also

- [Literature Management](literature-management.md) — the pool, the four collections, and where
  compiling sits in the paper lifecycle.
- [The Task System](task-system.md) — how `wiki.compile` and `wiki.link_concepts` are scheduled,
  retried, checkpointed and billed as steps of a library-build run.
- [Embedding & Retrieval](embedding-and-retrieval.md) — `wiki.link_concepts` also fills in missing
  paper-level and chunk vectors in the same step.
- [Core Concepts](concepts.md) — the skill system whose `wiki.compile` target is the one external
  injection point into the compile prompt.
