--------------------------- MODULE Statement ---------------------------
(* One theme's view, or one question's position, over its versions, as
   lestash PR #425 proposes its statuses. The transitions are those in
   lestash-14's summary of 27 Nov 2026; the doc lists only the statuses.

   #425 gives one scheme: hunch, to verify, held, decided, formally stated;
   then withdrawn or superseded. Each version here has that one `status`,
   and a ghost `stance`: the last of the first five it held. *)
EXTENDS Naturals, Sequences

CONSTANTS MaxVer,
          AllowDoubt,          \* held -> to verify
          RefineMakesVersion,  \* #425: "each change is a new version"
          RestartAfterWithdraw \* a new view after a withdrawn one

VARIABLES v   \* a sequence of [status, stance], oldest first
vars == <<v>>

Live  == {"hunch", "toverify", "held", "decided", "formal"}
Ended == {"withdrawn", "superseded"}
Up    == [hunch |-> "toverify", toverify |-> "held",
          held |-> "decided", decided |-> "formal"]

Ver(s) == [status |-> s, stance |-> s]
Last   == v[Len(v)]
IsLive == Len(v) > 0 /\ Last.status \in Live
Room   == Len(v) < MaxVer

Init == v = <<>>

\* Replace the current version's status with an ended one.
End(s) == [v EXCEPT ![Len(v)] = [@ EXCEPT !.status = s]]

\* The first view, or one after a withdrawn view. #425 doesn't say which
\* status a new view starts at, so any.
Start == /\ Room
         /\ \/ Len(v) = 0
            \/ RestartAfterWithdraw /\ Len(v) > 0 /\ Last.status = "withdrawn"
         /\ \E s \in Live : v' = Append(v, Ver(s))

\* Move the status one step, in place or as a new version.
Step(s) == IF RefineMakesVersion
           THEN Room /\ v' = Append(End("superseded"), Ver(s))
           ELSE v' = [v EXCEPT ![Len(v)] = Ver(s)]

Refine == IsLive /\ Last.status \in DOMAIN Up /\ Step(Up[Last.status])
Doubt  == AllowDoubt /\ IsLive /\ Last.status = "held" /\ Step("toverify")

\* Revise: a new version, linked to the old by revision_of. #425 doesn't say
\* which status the new version starts at, so any.
Revise == IsLive /\ Room
          /\ \E s \in Live : v' = Append(End("superseded"), Ver(s))
Withdraw == IsLive /\ v' = End("withdrawn")

Next == Start \/ Refine \/ Doubt \/ Revise \/ Withdraw
Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
TypeOK == v \in Seq([status : Live \cup Ended, stance : Live])

\* At most one version is current, and only the last one.
OneCurrent == \A i \in 1..Len(v) : v[i].status \in Live => i = Len(v)

\* An old version still says what it held when it ended.
HistoryReadable == \A i \in 1..Len(v) : v[i].status \in Ended =>
                     v[i].status = v[i].stance

\* Reachability: each is expected to be violated.
NeverFormal   == \A i \in 1..Len(v) : v[i].stance # "formal"
NeverReopened == ~(Len(v) > 1 /\ \E i \in 1..Len(v) - 1 :
                     v[i].status = "withdrawn")
=============================================================================
