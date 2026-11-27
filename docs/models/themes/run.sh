#!/usr/bin/env bash
# Runs each case of Themes.tla and Statement.tla with TLC 2.19.
# "designed" is the model as the doc now reads, and must pass. Each other
# case switches one fix off, or checks a state is reachable, and must be
# violated. One line per case: name, outcome, expected. TRACE=1 prints
# the trace of each violated case. TLA2TOOLS overrides the jar's path.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
jar="${TLA2TOOLS:-$HOME/.cache/tla2tools-936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88.jar}"
[[ -f $jar ]] || { echo "TLC jar not found: $jar (set TLA2TOOLS)"; exit 2; }
fail=0
declare -A defs
defs[Themes]="Items={1,2,3} Slots={1,2} PinReconsidered=TRUE OverlapCoversPinned=TRUE WithdrawnBlocksDrop=TRUE KeepOneItem=TRUE BothWays=TRUE ReconsiderClears=TRUE"
defs[Statement]="MaxVer=3 TwoFields=TRUE DoubtAnyStep=TRUE RestartAfterWithdraw=TRUE"

run() { # module name expected kind check overrides...
  local m=$1 n=$2 e=$3 k=$4 c=$5; shift 5
  local w; w=$(mktemp -d); cp "$here/$m.tla" "$w/"
  declare -A val; local kv
  for kv in ${defs[$m]} "$@"; do val[${kv%%=*}]=${kv#*=}; done
  { echo "SPECIFICATION Spec"; echo "INVARIANT TypeOK"
    for key in "${!val[@]}"; do echo "CONSTANT $key = ${val[$key]}"; done
    if [[ $k == ALL ]]; then echo "$c"; else echo "$k $c"; fi; } > "$w/$m.cfg"
  local out r st
  out=$(cd "$w" && java -XX:+UseParallelGC -cp "$jar" tlc2.TLC -deadlock -workers auto "$m" 2>&1)
  if grep -q "No error has been found" <<<"$out"; then r=pass
  elif grep -qi "is violated" <<<"$out"; then r=violated; else r=error; fi
  st=$(grep -oE '[0-9,]+ distinct states found' <<<"$out" | head -1)
  local ok=ok; [[ $r == "$e" ]] || { ok=UNEXPECTED; fail=1; }
  printf '%-28s %-9s %-11s %s\n' "$n" "$r" "$ok" "$st"
  [[ $r == error ]] && echo "$out" | tail -20
  [[ -n "${TRACE:-}" && $r == violated ]] && echo "$out" | sed -n '/violated/,/distinct states/p' | head -80
  rm -rf "$w"
}

ALLT="INVARIANT ThinkingKept OverlapHolds NoTwinSuggestions DecidedHoldItems
PROPERTY RerunLeavesAlone"
ALLS="INVARIANT OneCurrent
PROPERTY HistoryKept EndKeepsStance"

if [[ $# -gt 0 ]]; then run "$@"; exit; fi
#        module    case                    expect   kind      check
run Themes    designed                pass     ALL       "$ALLT"
run Themes    rerun-deletes-reconsidered violated PROPERTY RerunLeavesAlone PinReconsidered=FALSE
run Themes    withdrawn-unblocks-drop violated INVARIANT ThinkingKept       WithdrawnBlocksDrop=FALSE
run Themes    empty-kept-theme        violated INVARIANT DecidedHoldItems   KeepOneItem=FALSE
run Themes    rerun-ignores-reconsidered violated INVARIANT NoTwinSuggestions OverlapCoversPinned=FALSE
run Themes    reconsider-keeps-twins  violated INVARIANT NoTwinSuggestions  ReconsiderClears=FALSE
run Themes    overlap-one-way         violated INVARIANT OverlapHolds       BothWays=FALSE
run Themes    reach-drop-reconsider   violated INVARIANT NeverDroppedThenReconsidered
run Themes    reach-archived-coll     violated INVARIANT NeverArchivedCollOnArchivedTheme
run Statement designed                pass     ALL       "$ALLS"
run Statement one-status-field        violated PROPERTY  EndKeepsStance     TwoFields=FALSE
run Statement reach-formal            violated INVARIANT NeverFormal
run Statement reach-reopened          violated INVARIANT NeverReopened
run Statement reach-doubted-decided   violated PROPERTY  NeverDoubtedDecided
run Statement no-restart              pass     INVARIANT NeverReopened      RestartAfterWithdraw=FALSE
run Statement held-only-doubt         pass     PROPERTY  NeverDoubtedDecided DoubtAnyStep=FALSE
exit $fail
