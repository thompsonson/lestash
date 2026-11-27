# LeStash themes: a checked model

**From: the atomicguard session.** A model of lestash PR #425, "Themes, questions and
positions" (`docs/content-and-activity-ontology-design.md` at 1545b88), with
`packages/lestash/src/lestash/core/themes.py` for the rerun as built.

## Files

- `Themes.tla`: the theme lifecycle, the rerun, items, and Drop's guard.
- `Statement.tla`: one view or position over its versions.
- `run.sh`: every case, with TLC 2.19 (atomicguard's pinned build). `bash run.sh`.

## What is modelled

- Theme statuses: none (no row), suggested, kept, archived, dismissed.
- User actions: Keep, Dismiss, Reconsider, Set aside, Resume, Drop, add or drop an item.
- On a kept theme: write or withdraw a view, add a question, add, move or delete a
  collection.
- Collection moves: current ↔ reference, either → archived, archived → current.
- A rerun finds one group, as `find_themes` treats each group:
  - skip it if it overlaps a kept, archived or dismissed theme by half;
  - keep a suggestion with the same items;
  - else add it; then delete every suggestion not found again.
- View or position: hunch → to verify → held → decided → formally stated; held → to
  verify; Revise; Withdraw.

The view-and-position transitions come from lestash-14's summary. The doc lists only
the statuses.

## Results (Items = 3, Slots = 2, MaxVer = 3)

| Case | Outcome | States |
|---|---|---|
| Themes, as written | DecidedHoldItems violated | 192 |
| Themes, with a kept theme keeping one item | pass | 66,515 |
| Rerun as `themes.py` does it today | RerunLeavesAlone violated | 394 |
| A withdrawn view doesn't count for Drop | ThinkingKept violated | 1,259 |
| Two suggestions at once | NoTwinSuggestions violated | 493 |
| …with reconsidered themes in the overlap rule | still violated | 599 |
| Statement, one current version | pass | 1,111 |
| Statement, an old version's status | HistoryReadable violated | 11 |
| No new view after a withdrawn one | NeverReopened holds: a dead end | 311 |

Reachability checks pass: formally stated is reached; Drop then Reconsider is reached;
an archived collection on an archived theme is reached.

## Findings

### 1. Conflicting rules

- **A rerun deletes a reconsidered theme.**
  - #425 says a reconsidered theme "stays until you decide again".
  - `find_themes` deletes every suggestion not found again, with no mark for reconsidered.
  - The design needs a mark on the row, and the rerun must skip marked rows.
- **One status scheme can't record both stance and version.**
  - Superseded and withdrawn replace a version's stance, such as held.
  - So an old version no longer says what you held, which defeats "shows what you learned".
  - Two fields fit: the stance, and whether the version is current, superseded or withdrawn.

### 2. Missing rules

- **A kept theme can lose all its items.** Nothing stops dropping the last one.
  - An empty theme blocks no rerun suggestion, though the doc says decided themes do.
- **Does a withdrawn view, or an old version, count for Drop?**
  - If not, Drop dismisses a theme that still holds your past thinking.
  - If so, any theme that ever had a view can never be dropped.
- **Which status does a new view, or a revised one, start at?** The doc doesn't say.
- **After Withdraw, can you write a new view?**
  - If not, a theme whose view was withdrawn can never have one again.
- **Can an archived theme be edited?** Its view, questions, items and collections.
- **Can a question or position be removed?** No action exists.
  - Without one, a theme with a question can only move between kept and archived.
- **Is a reconsidered theme renamed by a rerun?** Today a rerun renames any suggestion.
  - A theme you named and then dropped would lose your name.
- **Can one collection belong to two themes?** "Links to any number" doesn't say.
- **Can a decided theme be doubted?** Only held → to verify is listed.

### 3. Overlap rule

- **Two suggestions can cover the same items.**
  - Reconsider doesn't check overlap, so it can bring back a theme inside an open suggestion.
  - A rerun can suggest a group that wholly contains a reconsidered theme.
- **The overlap is measured one way: the share of the new group.**
  - A new group can wholly contain a dismissed theme and still be suggested.
  - So "a rerun doesn't suggest them again as a new theme" holds only for half or more.

### 4. One possible clash

- `revision_of` joins a `document` or a `note` (the link table, line 325).
  - A view or a position is neither, unless it is stored as a note.

## Not found

- No unreachable status.
- Deadlock isn't checked (`-deadlock`): a rerun is always possible, so it tells nothing.
- The rerun never changes a kept, archived or dismissed theme (RerunLeavesAlone holds
  once reconsidered rows are marked).
