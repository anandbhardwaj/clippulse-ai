# Board status protocol (GitHub Projects v2)

Reference for `/implement-story` (and `/refine-story` when it needs to check statuses). The board's **Status** column is how the team sees where each story is. Keep it truthful.

## Status meanings

| Status | Meaning | Who moves it |
|---|---|---|
| Not started | Refined, nobody working on it | (default) |
| In progress | An agent or developer is implementing it now | `/implement-story` at start |
| QA | Implemented, committed **and pushed**, all checks green, waiting for human review/QA | `/implement-story` at finish |
| Impediment | Work cannot continue: unmet dependency, product decision needed, missing credentials | `/implement-story` when blocked |
| Done | Merged | Human, or a board workflow on merge/close |

Never move a card to Done. Names on the board may differ (`In Progress`, `In QA`, ...): match on the meaning, not the exact string.

**This repo's board** is user project #3, "ClipPulse AI Delivery" (`gh project list --owner <OWNER>`), and its Status options map like this:

| Meaning | Option name on the board |
|---|---|
| Not started | `Backlog` |
| In progress | `In Progress` |
| Impediment | `Impediment` (added by us, red) |
| QA | `Review / Quality Gate` (there is no literal "QA" option; do not create one) |
| Done | `Done` |

Option IDs change whenever the option list is edited, so always look them up with `field-list` at run time; never hard-code them.

## Prerequisites

The GitHub token needs the `project` scope. Check with `gh auth status`. If the scope is missing, **do not stop the story**; tell the user to run `! gh auth refresh -s project` (interactive), continue the implementation, and end your report with "Board not updated: missing `project` scope". Board updates are never a reason to skip or delay real work.

## Find the item

```
gh issue view <N> --json projectItems
```

Each entry has the `project` (`id`, `title`, `number`) and the current `status` (`name`, `optionId`). Then:

```
gh project field-list <PROJECT_NUMBER> --owner <OWNER> --format json     # find the field named "Status": its id and options[].{id,name}
gh project item-list  <PROJECT_NUMBER> --owner <OWNER> --format json --limit 200   # find the item whose content.number == N -> its item id
```

If the issue is on no board, add it (only when exactly one project exists for the owner; otherwise ask):

```
gh project list --owner <OWNER> --format json
gh project item-add <PROJECT_NUMBER> --owner <OWNER> --url <ISSUE_URL>
```

`<OWNER>` is the repository owner (`gh repo view --json owner -q .owner.login`).

## Move a card

```
gh project item-edit --project-id <PROJECT_ID> --id <ITEM_ID> --field-id <STATUS_FIELD_ID> --single-select-option-id <OPTION_ID>
```

Then re-read `gh issue view <N> --json projectItems` and confirm the status name changed. Report the move in your final reply.

## When to move (used by /implement-story)

1. **Start (end of Preflight, before writing code):** -> `In progress`.
2. **Finish (after commit, push and the completion comment):** -> `QA`. Push the feature branch first (never the default branch), because QA needs reviewable code. If pushing fails or the user said not to push, leave the card `In progress` and say why.
3. **Blocked:** -> `Impediment` and post a comment (see below) whenever you must stop, i.e. any of:
   - a dependency listed in the issue is not usable (its code is not in your branch and no safe fallback exists),
   - a gap has no safe fallback and needs a product/architecture decision,
   - credentials or a paid service are required that you do not have.
   Comment shape:
   ```
   ### 🚧 Impediment

   Blocked by: <#N or description>
   Why: <what cannot proceed and what was already tried>
   Needed to unblock: <decision / merge of #N / credential>
   State of work: <what is committed or drafted so far>
   ```
   When the blocker clears, the next `/implement-story` run moves the card back to `In progress`.
4. A gap that **has** a safe fallback does not block: post the gap comment, apply the fallback, keep going.

## Ensure the Impediment (and QA) status exists

Before the first move, check that the Status options include `In progress`, `QA` and `Impediment` (by meaning). If `Impediment` is missing, add it; if `QA` or `In progress` is missing, ask the user before changing the board.

Adding an option means updating the Status field's option list through GraphQL (`updateProjectV2Field` with `singleSelectOptions`, sent with `gh api graphql --input <file.json>` since the variables are nested). **Confirmed on this board: the call regenerates every option ID and clears the Status of every item.** So always:

1. Snapshot every item's current Status (`gh project item-list ... --format json`) and the full existing option list (names, colours, descriptions).
2. Run the mutation with all existing options plus the new one (`Impediment`, colour `RED`, description "Blocked: needs a decision, dependency or credential").
3. Re-read the field, then re-apply each item's previous Status by name using `item-edit`.
4. Verify every item's Status equals its snapshot; report any that do not.

If any step fails, stop and ask the user to add the option in the board's field settings (Status -> Add option) rather than retrying blindly. This mutation is not pre-approved in `allowed-tools`; expect a permission prompt.
