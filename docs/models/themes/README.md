# Themes: a checked model

A TLA+ model of "Themes, questions and positions" in
[`content-and-activity-ontology-design.md`](../../content-and-activity-ontology-design.md) (#340).
The rerun follows `packages/lestash/src/lestash/core/themes.py`.

The atomicguard session wrote the first version on 27 Nov 2026, against the doc as it then read.
Its findings changed the design; this version models the design after those changes.

## Files

- `Themes.tla`: the theme lifecycle, the rerun, items, Drop's guard and collections.
- `Statement.tla`: one view or position over its versions.
- `run.sh`: every case, with TLC 2.19. `bash run.sh`; set `TLA2TOOLS` to the jar's path if it isn't in `~/.cache`.

## What is modelled

- Theme statuses: none (no row), suggested, kept, archived, dismissed.
- Actions on a theme: Keep, Dismiss, Reconsider, Set aside, Resume, Drop, add or drop an item.
- On a kept theme: write or withdraw a view, add a question, add, move or delete a collection.
  - An archived theme is read-only until Resume.
  - A question is never deleted; withdrawing one keeps it, so it is modelled as staying.
- Collection statuses: current ↔ reference, either → archived, archived → current.
- A rerun finds one group, as `find_themes` treats each group:
  - skip it if it overlaps a kept, archived, dismissed or reconsidered theme by half, measured both ways;
  - keep a suggestion with the same items;
  - else add it; then delete every suggestion not found again, except reconsidered ones.
- A view or position: a stance (hunch → to verify → held → decided → formally stated, and one step back from any) and a state (current, superseded, withdrawn).
  - A new view starts at hunch; a revised one starts at the old one's stance.
  - A new view can follow a withdrawn one.

Names, the rename rule and collections shared between themes aren't modelled: none changes a status.

## The fixes each case protects

`designed` must pass; every other case switches one fix off, or looks for a state, and must be violated.

| Case | Fix switched off | Promise that breaks |
|---|---|---|
| Themes `designed` | none | all hold |
| `rerun-deletes-reconsidered` | a rerun skips reconsidered themes | RerunLeavesAlone |
| `withdrawn-unblocks-drop` | a withdrawn view still blocks Drop | ThinkingKept |
| `empty-kept-theme` | a kept theme keeps one item | DecidedHoldItems |
| `rerun-ignores-reconsidered` | reconsidered themes block overlapping suggestions | NoTwinSuggestions |
| `reconsider-keeps-twins` | Reconsider removes suggestions it overlaps | NoTwinSuggestions |
| `overlap-one-way` | overlap measured both ways | OverlapHolds |
| Statement `designed` | none | all hold |
| `one-status-field` | stance and state are two fields | EndKeepsStance |

Reachability: Drop then Reconsider; an archived collection on an archived theme; formally stated; a new view after a withdrawn one; doubting a decided view.

## Not checked

- Deadlock (`-deadlock`): a rerun is always possible, so it tells nothing.
- Sizes: Items = 3, Slots = 2, MaxVer = 3.
