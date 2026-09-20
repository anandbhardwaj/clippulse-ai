---
name: refine-story
description: Refine a rough GitHub issue into an enterprise-grade user story (user story statement, key users, Gherkin acceptance criteria, technical design) and update it on GitHub. Use when the user says "refine story", "refine issue #N", or wants a backlog item enriched. Story refinement only; never touches code.
argument-hint: "[issue number]"
disable-model-invocation: true
allowed-tools: Bash(gh issue list:*), Bash(gh issue view:*), Bash(gh issue edit:*), Bash(git ls-files:*), Bash(git status:*), Read, Glob, Grep, Write
---

# Refine Story

You are an Agile Technical Product Manager. Turn a rough GitHub issue into a fully specified story and write it back to the issue. **Do not modify any repository code, config or docs.** The only side effect is `gh issue edit`.

Target issue: `$ARGUMENTS` (if empty, list open issues and ask which one to refine).

## Workflow

1. **Find and read the issue**
   - `gh issue list --state open`
   - `gh issue view <N>`: capture the title, the rough story and every existing acceptance criterion. Nothing the author wrote may be silently dropped or contradicted; if you must change a stated rule, flag it under Open Questions.

2. **Gather context (read-only)** so the design fits the codebase, not a generic template
   - Read the neighbouring issues (`gh issue view <other> --json body -q .body`) to learn the upstream and downstream data contracts.
   - Read `README.md`, `pyproject.toml`, `src/config.py`, and any existing stub or module the story will live in. Match existing conventions (error classes, config style, dependencies already declared, test layout).

3. **Draft the enriched body** with exactly this schema, in this order:

   1. `## User Story`: formal *As a / I want / So that*, plus a one-line pipeline position (upstream -> this story -> downstream).
   2. `## Key Users and Benefits`: table of user -> explicit benefit. Include the downstream consuming module and the developer/maintainer.
   3. `## Acceptance Criteria (Gherkin)`: **5-8** strict scenarios in Given / When / Then. Must cover:
      - happy path
      - output contract / schema and precision
      - malformed inputs or malformed upstream/LLM output (and recovery vs. failure)
      - rate limits and transient failures (retry/backoff, then exhaustion)
      - contract validation of boundaries and types
      - fail-fast on invalid caller input
      Use `Scenario Outline` + `Examples` tables for boundary cases. Each criterion must be objectively testable (concrete numbers, exception names, call counts).
      If a category genuinely does not apply (e.g. rate limits for a story with no network calls), do not invent a criterion: add an italic line under the ACs, *"Rate limits: not applicable because ..."*, and cover a relevant risk instead (e.g. hostile input, offline operation).
      If the story is already partly implemented, add a "Current state vs. target" table (before the ACs) and turn each gap into an acceptance criterion.
   4. `## Technical Solution Design`: module architecture (files and call flow), class names, data types and signatures, error-handling hierarchy plus a retry/behaviour table, config additions, and a test plan mapping 1:1 to the ACs.
   5. `## Dependencies`: **always present, always in this exact machine-readable shape** (see "Independence rules" below), because `/implement-story` reads it to decide between `In progress`, `QA` and `Impediment`:
      ```
      ## Dependencies
      Independent: yes | no
      Depends on: #<N> - <exact thing needed, e.g. "TranscriptChunk model in src/transcriber.py"> - Fallback if not merged: <stub/fake behind the agreed contract, or "none: story is blocked">
      Blocks: #<M> - <what it consumes from this story>
      ```
      Use one `Depends on:` / `Blocks:` line per issue; write `Depends on: none` / `Blocks: none` when empty.
   6. `## Out of Scope and Open Questions`: explicit non-goals and any decisions the Product Owner must make (e.g. gaps in the mandated schema).
   7. `## Definition of Done`: checklist including the repo's verification commands (`uv run pytest tests/`, `uv run ruff check .`, mypy where relevant).

   **Independence rules.** Stories should be as independent as possible, so any of them can be implemented, tested and moved to QA without waiting for another:
   - Prefer a design where the story owns everything it needs, or depends on an upstream only through a **small, explicit contract** (a type, a function signature, a file format) written out in the story's own design section. Then the story can be built and tested against a fake of that contract.
   - If a dependency is unavoidable, list it, name the exact artifact needed (file, class, field), and give a **fallback** that lets work proceed (e.g. "define the model locally per the contract in #2, replace the import when #2 merges"). Write `Fallback: none: story is blocked` only when there truly is none, because that is what sends the card to Impediment.
   - Check for **cycles** and for hidden coupling (shared config keys, shared helper modules, ordering of merges). Call them out.
   - If two stories are entangled beyond a clean contract, say so in Open Questions and recommend merging or re-splitting them instead of hiding the coupling.
   - Derive `Blocks:` from the neighbouring issues you read. Do not edit those issues; if another issue's Dependencies section is now out of date, tell the user which one.

   Style rules: plain ASCII punctuation (no emoji), no invented dependencies, no code implementation beyond signatures and short illustrative snippets.

4. **Write it to GitHub**
   - Save the draft to a temp file in the scratchpad directory, then run `gh issue edit <N> --body-file <file>`.
   - Prefer `--body-file` over inline `--body "..."`: backticks, JSON and code fences break shell quoting, especially on Windows.

5. **Verify and report**
   - `gh issue view <N> --json body -q .body` to confirm the update landed.
   - Run `git status --short` and confirm the working tree is unchanged by this task.
   - Reply with: the issue URL, a short summary of what was added, the story's **independence verdict** (independent, or exactly what it depends on and whether each dependency has a fallback), and the **open questions** that need the user's decision. Offer to refine the next issue.
   - Board statuses (`In progress`, `QA`, `Impediment`) are set by `/implement-story`, not here. If you notice the project board lacks an `Impediment` status, mention it; `/implement-story` will add it (procedure in `../implement-story/board.md`).
