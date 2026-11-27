---------------------------- MODULE Themes ----------------------------
(* LeStash themes, as lestash PR #425 designs them ("Themes, questions and
   positions", docs/content-and-activity-ontology-design.md at 1545b88).

   Abstractions:
   - A rerun of `lestash db themes` finds at most one group of items. Groups
     from one Louvain run are disjoint, so one group shows every rule a rerun
     applies to it.
   - A theme's view, questions and collection are reduced to what Drop's
     guard asks: is there one? The view and position statuses are in
     Statement.tla.
   - A theme row is a slot. A deleted suggestion frees its slot; a new one
     takes a free slot, so it gets a new id, as in the code. *)
EXTENDS Naturals, FiniteSets

CONSTANTS Items, Slots,
          PinReconsidered,     \* doc: a rerun leaves a reconsidered theme alone
          OverlapCoversPinned, \* the overlap rule also counts reconsidered themes
          WithdrawnBlocksDrop, \* a withdrawn view still counts as "a view"
          KeepOneItem          \* a kept theme can't drop its last item

VARIABLES st, its, pin, view, q, coll, ran, lost
vars == <<st, its, pin, view, q, coll, ran, lost>>

Status   == {"none", "suggested", "kept", "archived", "dismissed"}
Decided  == {"kept", "archived", "dismissed"}
CollSt   == {"none", "current", "reference", "archived"}

\* The share of g that d holds too (themes.py _overlap): at least half?
Half(g, d) == g # {} /\ 2 * Cardinality(g \cap d) >= Cardinality(g)

Thought(t) == \/ view[t] = "live"
              \/ view[t] = "withdrawn" /\ WithdrawnBlocksDrop
              \/ q[t]
              \/ coll[t] # "none"
\* Anything of the user's, a withdrawn view included, which the doc keeps.
AnyThought(t) == view[t] # "none" \/ q[t] \/ coll[t] # "none"

TypeOK == /\ st \in [Slots -> Status]
          /\ its \in [Slots -> SUBSET Items]
          /\ pin \in [Slots -> BOOLEAN]
          /\ view \in [Slots -> {"none", "live", "withdrawn"}]
          /\ q \in [Slots -> BOOLEAN]
          /\ coll \in [Slots -> CollSt]
          /\ ran \in BOOLEAN /\ lost \in BOOLEAN

Init == /\ st = [t \in Slots |-> "none"]
        /\ its = [t \in Slots |-> {}]
        /\ pin = [t \in Slots |-> FALSE]
        /\ view = [t \in Slots |-> "none"]
        /\ q = [t \in Slots |-> FALSE]
        /\ coll = [t \in Slots |-> "none"]
        /\ ran = FALSE /\ lost = FALSE

\* Set st[t] to s; record a loss if the user's thinking leaves the theme's lists.
Move(t, s) == /\ st' = [st EXCEPT ![t] = s]
              /\ lost' = (lost \/ (s \in {"none", "dismissed"} /\ AnyThought(t)))

----------------------------------------------------------------------------
(* The rerun (themes.py find_themes, plus #425's change for reconsidered). *)
Protected(t) == pin[t] /\ st[t] = "suggested" /\ PinReconsidered
Blockers == {t \in Slots : st[t] \in Decided
                           \/ (OverlapCoversPinned /\ Protected(t))}

Rerun(g) ==
  LET skip  == g = {} \/ \E d \in Blockers : Half(g, its[d])
      same  == {t \in Slots : st[t] = "suggested" /\ its[t] = g}
      found == IF skip THEN {} ELSE same
      gone  == {t \in Slots : st[t] = "suggested" /\ t \notin found
                              /\ ~Protected(t)}
      free  == {t \in Slots : st[t] = "none"} \cup gone
      new   == IF skip \/ same # {} \/ free = {} THEN {}
               ELSE {CHOOSE t \in free : TRUE}
  IN /\ st' = [t \in Slots |-> IF t \in new THEN "suggested"
                               ELSE IF t \in gone THEN "none" ELSE st[t]]
     /\ its' = [t \in Slots |-> IF t \in new THEN g
                                ELSE IF t \in gone THEN {} ELSE its[t]]
     /\ pin' = [t \in Slots |-> IF t \in new \cup gone THEN FALSE ELSE pin[t]]
     /\ view' = [t \in Slots |-> IF t \in new \cup gone THEN "none" ELSE view[t]]
     /\ q' = [t \in Slots |-> IF t \in new \cup gone THEN FALSE ELSE q[t]]
     /\ coll' = [t \in Slots |-> IF t \in new \cup gone THEN "none" ELSE coll[t]]
     /\ ran' = TRUE
     /\ lost' = (lost \/ \E t \in gone : AnyThought(t))

----------------------------------------------------------------------------
(* The user's decisions. *)
Keep(t)       == st[t] = "suggested" /\ Move(t, "kept")
                 /\ pin' = [pin EXCEPT ![t] = FALSE]
                 /\ UNCHANGED <<its, view, q, coll>>
Dismiss(t)    == st[t] = "suggested" /\ Move(t, "dismissed")
                 /\ pin' = [pin EXCEPT ![t] = FALSE]
                 /\ UNCHANGED <<its, view, q, coll>>
Reconsider(t) == st[t] = "dismissed" /\ Move(t, "suggested")
                 /\ pin' = [pin EXCEPT ![t] = TRUE]
                 /\ UNCHANGED <<its, view, q, coll>>
SetAside(t)   == st[t] = "kept" /\ Move(t, "archived")
                 /\ UNCHANGED <<its, pin, view, q, coll>>
Resume(t)     == st[t] = "archived" /\ Move(t, "kept")
                 /\ UNCHANGED <<its, pin, view, q, coll>>
Drop(t)       == st[t] = "kept" /\ ~Thought(t) /\ Move(t, "dismissed")
                 /\ UNCHANGED <<its, pin, view, q, coll>>

\* Editing a kept theme's items.
AddItem(t, i)  == st[t] = "kept" /\ i \notin its[t]
                  /\ its' = [its EXCEPT ![t] = @ \cup {i}]
                  /\ UNCHANGED <<st, pin, view, q, coll, lost>>
DropItem(t, i) == st[t] = "kept" /\ i \in its[t]
                  /\ (KeepOneItem => Cardinality(its[t]) > 1)
                  /\ its' = [its EXCEPT ![t] = @ \ {i}]
                  /\ UNCHANGED <<st, pin, view, q, coll, lost>>

\* The user's thinking on a kept theme. The doc doesn't say whether an
\* archived theme can be edited; this model allows edits only when kept.
WriteView(t)    == st[t] = "kept" /\ view[t] # "live"
                   /\ view' = [view EXCEPT ![t] = "live"]
                   /\ UNCHANGED <<st, its, pin, q, coll, lost>>
WithdrawView(t) == st[t] = "kept" /\ view[t] = "live"
                   /\ view' = [view EXCEPT ![t] = "withdrawn"]
                   /\ UNCHANGED <<st, its, pin, q, coll, lost>>
AddQuestion(t)  == st[t] = "kept" /\ ~q[t]
                   /\ q' = [q EXCEPT ![t] = TRUE]
                   /\ UNCHANGED <<st, its, pin, view, coll, lost>>

\* Collection statuses (proposed): current <-> reference, either -> archived,
\* archived -> current. Deleting a collection exists today (#318).
CollNext == [current |-> {"reference", "archived"},
             reference |-> {"current", "archived"},
             archived |-> {"current"}]
AddColl(t)    == st[t] = "kept" /\ coll[t] = "none"
                 /\ coll' = [coll EXCEPT ![t] = "current"]
                 /\ UNCHANGED <<st, its, pin, view, q, lost>>
CollMove(t)   == st[t] = "kept" /\ coll[t] # "none"
                 /\ \E s \in CollNext[coll[t]] : coll' = [coll EXCEPT ![t] = s]
                 /\ UNCHANGED <<st, its, pin, view, q, lost>>
DeleteColl(t) == st[t] = "kept" /\ coll[t] # "none"
                 /\ coll' = [coll EXCEPT ![t] = "none"]
                 /\ UNCHANGED <<st, its, pin, view, q, lost>>

User == /\ ran' = FALSE
        /\ \E t \in Slots :
             \/ Keep(t) \/ Dismiss(t) \/ Reconsider(t)
             \/ SetAside(t) \/ Resume(t) \/ Drop(t)
             \/ \E i \in Items : AddItem(t, i) \/ DropItem(t, i)
             \/ WriteView(t) \/ WithdrawView(t) \/ AddQuestion(t)
             \/ AddColl(t) \/ CollMove(t) \/ DeleteColl(t)

Next == (\E g \in SUBSET Items : Rerun(g)) \/ User
Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
(* What the design promises. *)

\* "Your thinking is never lost": no theme with the user's thinking is
\* dismissed or deleted.
ThinkingKept == ~lost

\* After a rerun, no ordinary suggestion overlaps a decided theme by half.
OverlapHolds == ran => \A s, d \in Slots :
                  (st[s] = "suggested" /\ ~pin[s] /\ st[d] \in Decided)
                    => ~Half(its[s], its[d])

\* No two suggestions offered at once cover the same items by half.
NoTwinSuggestions == \A s, u \in Slots :
                       (s # u /\ st[s] = "suggested" /\ st[u] = "suggested")
                         => ~Half(its[s], its[u])

\* A decided theme holds items, so it keeps a rerun from suggesting them.
DecidedHoldItems == \A t \in Slots : st[t] \in Decided => its[t] # {}

\* A rerun leaves decided themes and reconsidered suggestions alone.
RerunLeavesAlone ==
  [][(\E g \in SUBSET Items : Rerun(g)) =>
       \A t \in Slots :
         (st[t] \in Decided \/ (pin[t] /\ st[t] = "suggested"))
           => (st'[t] = st[t] /\ its'[t] = its[t])]_vars

(* Reachability: each is expected to be violated, which shows the state
   can be reached. *)
NeverDroppedThenReconsidered == ~\E t \in Slots : pin[t] /\ view[t] = "none"
                                    /\ st[t] = "suggested" /\ q[t] = FALSE
NeverArchivedCollOnArchivedTheme ==
  ~\E t \in Slots : st[t] = "archived" /\ coll[t] = "archived"
=============================================================================
