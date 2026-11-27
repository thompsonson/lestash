--------------------------- MODULE Statement ---------------------------
(* One theme's view, or one question's position, over its versions, as
   "Themes, questions and positions" designs them (#340, #425).

   Each version has two fields:
   - stance: hunch, to verify, held, decided, formally stated;
   - state: current, superseded or withdrawn.
   First written by the atomicguard session with a single status field; the
   fixes its findings led to are switched on by the constants below, and
   run.sh switches each off in turn. *)
EXTENDS Naturals, Sequences

CONSTANTS MaxVer,
          TwoFields,           \* ending a version keeps its stance
          DoubtAnyStep,        \* step back one level from any stance
          RestartAfterWithdraw \* a new view after a withdrawn one

VARIABLES v   \* a sequence of [state, stance], oldest first
vars == <<v>>

Stances == {"hunch", "toverify", "held", "decided", "formal"}
States  == {"current", "superseded", "withdrawn"}
Up      == [hunch |-> "toverify", toverify |-> "held",
            held |-> "decided", decided |-> "formal"]
Down    == [toverify |-> "hunch", held |-> "toverify",
            decided |-> "held", formal |-> "decided"]

Ver(s)    == [state |-> "current", stance |-> s]
Last      == v[Len(v)]
IsCurrent == Len(v) > 0 /\ Last.state = "current"
Room      == Len(v) < MaxVer

Init == v = <<>>

\* End the current version. With one field, as first designed, the ended
\* state took the stance's place; here that is a lost stance ("hunch").
End(s) == [v EXCEPT ![Len(v)] =
             [state |-> s, stance |-> IF TwoFields THEN @.stance ELSE "hunch"]]

\* A new view starts at hunch: the first, or one after a withdrawn view.
Start == /\ Room
         /\ \/ Len(v) = 0
            \/ RestartAfterWithdraw /\ Len(v) > 0 /\ Last.state = "withdrawn"
         /\ v' = Append(v, Ver("hunch"))

\* Refine and doubt change the current version's stance in place.
SetStance(s) == v' = [v EXCEPT ![Len(v)] = [@ EXCEPT !.stance = s]]
Refine == IsCurrent /\ Last.stance \in DOMAIN Up /\ SetStance(Up[Last.stance])
Doubt  == IsCurrent /\ Last.stance \in DOMAIN Down
          /\ (DoubtAnyStep \/ Last.stance = "held")
          /\ SetStance(Down[Last.stance])

\* Revise: a new version that replaces the old. It starts at the old one's
\* stance, which refine and doubt then change.
Revise == IsCurrent /\ Room
          /\ v' = Append(End("superseded"), Ver(Last.stance))
Withdraw == IsCurrent /\ v' = End("withdrawn")

Next == Start \/ Refine \/ Doubt \/ Revise \/ Withdraw
Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
TypeOK == v \in Seq([state : States, stance : Stances])

\* At most one version is current, and only the last one.
OneCurrent == \A i \in 1..Len(v) : v[i].state = "current" => i = Len(v)

\* An ended version never changes, so it still says what it held.
HistoryKept == [][\A i \in 1..Len(v) :
                    v[i].state # "current" => v'[i] = v[i]]_vars
\* Ending a version keeps the stance it had.
EndKeepsStance == [][IsCurrent /\ Len(v') >= Len(v)
                     /\ v'[Len(v)].state # "current"
                       => v'[Len(v)].stance = Last.stance]_vars

\* Reachability: each is expected to be violated.
NeverFormal    == \A i \in 1..Len(v) : v[i].stance # "formal"
NeverReopened  == ~(Len(v) > 1 /\ \E i \in 1..Len(v) - 1 :
                      v[i].state = "withdrawn")
\* A decided view is never doubted back to held (an action property).
NeverDoubtedDecided == [][~(IsCurrent /\ Last.stance = "decided"
                            /\ Len(v') = Len(v)
                            /\ v'[Len(v)].stance = "held")]_vars
=============================================================================
