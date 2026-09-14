# Discipline packs

Polaris reads every paper into a **method card**: what the work was for, how it did it, and
the details you would need to judge or reproduce it. The built-in card is shaped for machine
learning — `purpose`, `mechanism`, `baseline`, `dataset`, `protocol`. For a structural
engineer those last three are the wrong questions. There is no dataset; there is a specimen,
a load case, an analysis, and something the result was checked against.

A **discipline pack** replaces those fields with the ones your field uses. It is a YAML file.
Writing one requires no code and no fork.

Four ship with Polaris — structural engineering, wet-lab biology, synthetic chemistry and
materials, clinical research — and they are meant to be read as templates as much as used.

## Turning one on

A pack applies to a **library**, not to the whole platform. Open the library's governance
tab and pick a discipline. Extraction from then on uses that pack's fields.

Papers already in the library keep the cards they have until they are enriched again; at that
point the discipline card is extracted alongside the built-in one, and the library starts
showing the discipline's reading. Nothing is rewritten retroactively.

Scoping to a library is deliberate. Installing a pack should not make every paper in every
library pay for an extra extraction in a field it has nothing to do with.

## Writing your own

Drop a `.yaml` file into `disciplines/` under the data directory. It appears in the picker
immediately — no restart. A file whose `name` matches a built-in pack overrides it, so the
shipped packs are a starting point you can edit rather than a ceiling.

```yaml
name: structural           # the value stored on the library; [a-z0-9][a-z0-9_-]*
title: 结构工程             # what the picker shows
description: >-
  面向结构/土木方向的抽取口径：把论文的做法按「结构对象 — 作用 — 分析手段 — 验证」
  拆开，替代以数据集与基线为中心的机器学习口径。

schemas:
  - id: method             # 名为 method 的这条参与方法库检索
    version: 1
    prompt: |
      POLARIS_EXTRACT_METHOD
      你是结构工程论文的方法卡抽取器。……
      {fields_spec}
      其中 "purpose" 只写这项工作要解决的结构问题（不写怎么做）；
      ……
      只输出一个 JSON 对象，键为上述字段名，另加 "confidence"（0 到 1）。
    fields:
      - name: purpose
        label: 目的
        kind: text
        max_len: 400
      - name: structure
        label: 结构对象
        kind: text
        max_len: 400
      - name: actions
        label: 作用与工况
        kind: list
        max_len: 200
        max_items: 5
```

### The rules, and why each exists

**`{fields_spec}` must appear in the prompt.** The field list handed to the model is
generated from `fields`, never written by hand. Written by hand, the two drift the first time
someone adds a field, and the drift is invisible — the model simply stops being told about it.

**A schema named `method` must keep `purpose` and `mechanism`.** Those two axes are what the
method library retrieves on: *same purpose, different mechanism* is a cross-disciplinary
question, and a card without them is extracted, stored, and never findable. Omitting them
fails at load rather than at search time.

Everything else is yours. Replace `baseline` and `dataset` with whatever your field actually
reports.

**Field kinds** are `text`, `list`, and `entries`:

```yaml
- name: performance
  label: 性能指标
  kind: entries            # 每条是一组白名单键
  max_items: 8
  entry_keys:
    - name: metric         # 指标名
      max_len: 80
    - name: value          # 数值与单位
      max_len: 80
    - name: condition      # 测试条件
      max_len: 160
```

Use `entries` when a value is only meaningful with its context. *"82%"* alone is not a
result; *"yield, 82%, in 1 M KOH"* is. The synthesis and clinical packs both use it — the
latter for `measure / effect / interval`, because whether a confidence interval crosses unity
is the thing worth comparing across studies.

There is no numeric kind yet, so a yield is stored as text. It displays correctly and cannot
be sorted on.

**`label`** is what the interface shows. It is not translated — it is part of your pack,
written in the language you work in. Polaris translating a domain term it does not understand
would be worse than showing yours. Leave it out and the field name is used, which reads fine
in English and looks half-finished in Chinese.

**Unknown keys are refused**, not ignored. A typo in `promt:` fails loudly instead of quietly
leaving your prompt at its default.

**Schema ids are namespaced.** Your `method` becomes `structural.method` internally, so two
packs can both call theirs `method` without colliding. A pack schema also inherits the model
routing of the built-in schema it shares a name with: `<pack>.method` runs wherever
`extract_method` is routed, so pointing method extraction at a stronger model applies to your
pack too.

**A broken pack is skipped, not fatal.** One bad indentation in a hand-written file does not
stop the others from loading or take the picker down.

## Where the fields show up

Once a library declares a discipline, its pack's fields appear everywhere the built-in ones
would:

- **method cards** in the method library, under your labels;
- **the comparison table**, when you put several papers side by side — the rows become your
  axes rather than `baseline` and `dataset` reading *"not extracted"* in every column;
- **CSV export** of that table.

The two cross-disciplinary axes keep working throughout, which is what lets a structural paper
and a chemistry paper still be found by the same *"same purpose, different mechanism"* query.

## What a pack does not change

Relevance scoring, the daily pool, and the wiki are unaffected — a pack changes how a paper is
**read**, not which papers arrive or how they are judged against your direction. Those are
configured per library in its inclusion settings.
