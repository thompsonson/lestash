#!/usr/bin/env bash
# Runs each case of Themes.tla and Statement.tla with the TLC build
# atomicguard's models.lock pins. One line per case: name, outcome, states.
# TRACE=1 prints the trace of each violated case.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
jar="$HOME/.cache/tla2tools-936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88.jar"
declare -A defs
defs[Themes]="Items={1,2,3} Slots={1,2} PinReconsidered=TRUE OverlapCoversPinned=FALSE WithdrawnBlocksDrop=TRUE KeepOneItem=FALSE"
defs[Statement]="MaxVer=3 AllowDoubt=TRUE RefineMakesVersion=FALSE RestartAfterWithdraw=TRUE"

run() { # module name kind check overrides...
  local m=$1 n=$2 k=$3 c=$4; shift 4
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
  printf '%-28s %-9s %s\n' "$n" "$r" "$st"
  [[ $r == error ]] && echo "$out" | tail -20
  [[ -n "${TRACE:-}" && $r == violated ]] && echo "$out" | sed -n '/violated/,/distinct states/p' | head -80
  rm -rf "$w"
}

ALLT="INVARIANT ThinkingKept OverlapHolds NoTwinSuggestions DecidedHoldItems
PROPERTY RerunLeavesAlone"
ALLS="INVARIANT OneCurrent HistoryReadable"

if [[ $# -gt 0 ]]; then run "$@"; exit; fi
run Themes designed               ALL       "$ALLT"
run Themes code-today-rerun       PROPERTY  RerunLeavesAlone PinReconsidered=FALSE
run Themes code-today-lost        INVARIANT ThinkingKept     PinReconsidered=FALSE
run Themes withdrawn-unblocks     INVARIANT ThinkingKept     WithdrawnBlocksDrop=FALSE
run Themes twins                  INVARIANT NoTwinSuggestions
run Themes twins-pinned-overlap   INVARIANT NoTwinSuggestions OverlapCoversPinned=TRUE
run Themes empty-decided          INVARIANT DecidedHoldItems
run Themes keep-one-item          INVARIANT DecidedHoldItems KeepOneItem=TRUE
run Themes overlap                INVARIANT OverlapHolds
run Themes reach-drop-reconsider  INVARIANT NeverDroppedThenReconsidered
run Themes reach-archived-coll    INVARIANT NeverArchivedCollOnArchivedTheme
run Statement designed            INVARIANT OneCurrent
run Statement history             INVARIANT HistoryReadable
run Statement reach-formal        INVARIANT NeverFormal
run Statement reach-reopened      INVARIANT NeverReopened
run Statement no-restart          INVARIANT NeverReopened    RestartAfterWithdraw=FALSE
