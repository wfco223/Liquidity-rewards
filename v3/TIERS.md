# Tier engines — design draft (2026-09-25)

Stage 1 (the fairs, v3/tierfair.py) and stage 2 (the value, v3/tiervalue.py)
run; stage 3 (paper trading, v3/tierpaper.py) is built and waits for his
yes to deploy. Each stage needs the owner's yes.

## What the owner asked for (2026-09-24/25)
- "We should build a different tender/engine for each tier" — the four
  midterm tiers, built from the ground up ("It can borrow elements from
  the tender, but there were clearly flaws").
- Assume $1,000 of free cash; today's orders and positions are not
  its concern ("We'll figure out how to get there later").
- Money: a fixed share per tier, reset each day from that tier's
  expected value.
- All four switches off at the start; he turns each on.
- "We have to get dynamic fairs. Things are changing fast."
- "Everything can go past fair if it is +ev."
- Decisions "every second. It just needs to be smart enough to spot
  when that is good vs bad. Hard coding rules is effective to a point
  but these rules are too easy to exploit."

## The tiers (the exchange's program ledger, 2026-09-24 23:47Z)
| Tier | Program | $/day per event | Target | df | Markets |
|---|---|---|---|---|---|
| T1 | midterms_t1_* (control, balance of power, tossup Senate) | 1,125 | 25,000 | 0.30 | 22 |
| T2 | midterms_t2_* (competitive Senate/Gov, seat counts) | 450 | 15,000 | 0.25 | 57 |
| T3 | midterms_t3_* (coverage: gov, senate, House districts) | 190 | 10,000 | 0.20 | 54 |
| T4 · $5 | midterms_t4_* (House winners) | 5 | 2,000 | 0.40 | 829 |
| T4 · $2 | politics_t4_coverage_* (margins of victory, turnout, seat counts by state, attorneys general, secretaries of state, Supreme Court) | 2 | 2,000 | 0.40 | 5,009 |

Both Tier 4 programs are monitored (owner, 2026-09-25 "focus on getting
the tier 4 markets identified and monitored both $5 and $2"): their
~5,800 books come over a stream of their own — two connections, in
subscriptions of 100 markets as the exchange's docs require — into a
store only the tier fairs read, and they are worked out every 10 s
rather than every second. Of the $2 program's 5,009 markets, 3,803 are
House margin-of-victory buckets (about nine to a district), 337 and 330
governor and Senate margins, 316 turnout brackets, 109 state House
seat counts, and the rest down-ballot races.

A market's tier is its program's name, never how much it pays. While
a tier's engine is on, nothing else trades that tier's markets.

## Flaws of the current setup it is built to avoid
1. Several programs trading one market with different fairs and rules
   (tender, old engine, bonds, sweep).
2. Markets changing hands when the exchange changes pay (31 on 09-24).
3. Fills inferred from position changes (unbooked covers, flipped
   positions).
4. Orders moved and resized all day for pennies, on fixed thresholds.
5. Rules that cross the spread or buy back at a loss on their own.

## The fair — re-estimated every second
Inputs, each weighted by how well it has predicted the price over the
last days (measured, not set by hand):
- the book: the size-weighted price over the first N shares a side,
  not the touch, so one small order cannot move it;
- trade prints from the stream (last trade price, open interest);
- linked markets: the outcomes of one event sum to $1 (dem/rep pairs,
  seat-count buckets, combos against their parts);
- Silver's model where it has one.
The fair carries a confidence; low confidence rests further out.

## The value of an order — every spot, every second
- plus: the reward that second — our share of the side's pool when the
  book qualifies (Target Size; Max Spread when a program sets one); a
  market's first day in a program pays nothing;
- minus: the chance of a fill times the loss when filled, measured from
  our own fills (how far the fair moved against us in the minutes
  after), per tier and side;
- minus: the cost of the capital tied up;
- minus: the cost of moving — the moment both the old and the new
  order rest, and the exchange's limit on how fast we may place and
  cancel. NOT a place in line: rewards do not depend on it, and at the
  back of the line others fill first, which for us is a help (owner,
  2026-09-25: "I'm not sure what you meant by the cost of losing a
  place in line" — I had it wrong).
It rests the best SET of orders on each side — one price level,
several, or none (owner, 2026-09-25: "rest at the best or nothing
excludes the possibility that it may be better to rest multiple orders
at different price levels") — and changes the set when another beats
staying by more than moving costs. No fixed minutes or dollar gates.

## Defence against being gamed — numbers, not rules
- Book levels that appear and vanish quickly count for less, both in
  the fair and as company.
- Fills that lose money raise that market's measured fill cost at once.
- The only hard limits are money: each tier spends its own share, and
  no market takes more than a set part of it.

## Money
A $1,000 pot. Each night at midnight ET each tier's share = its
expected value over the sum of the tiers that are on.

## Records
Fills from the exchange's own trade record by order id. Positions from
the exchange. One exit per position, sized to what is held.

## Stage 2 — the value, read-only (owner, 2026-09-26 "We're going to build
things from the ground up. Focus on building")
The $1,000 is assumed, as he said; nothing waits for the money.

For every tier market with a book under five minutes old, each side, each
candidate price (a tick inside the touch where the spread allows, the
touch, three ticks behind it, and every resting level within 6c behind),
a day:
- reward: our share of the side's pool by the exchange's own arithmetic
  (scoring.py — size x df^ticks inside the Target Size window, a side
  under its Target Size paying nobody), nothing on a market's first day
  in its program;
- fill cost: fills a day x shares x the loss a share. Fills a day are
  MEASURED on paper spots (below) per tier, side, distance from the touch
  and queue ahead, from a prior worth one day; the loss a share is what
  a filled paper spot lost against the midpoint an hour later, per tier
  and side, pulled toward the market's own, never under his fill floor
  (2c), plus his concession rule past the fair;
- capital: collateral x his cost of capital (0.5% a day, the focus
  page's number).
Our own resting orders are taken out of the book first (the tender's and
the engine's; his hand's stay in as company) — the engine starts from
nothing.

The best SET: money goes out in $5 slices, each to the price whose next
slice adds the most value a dollar across the tier. The reward share
saturates, so each side stops by itself; a side under its Target Size may
be carried over it in one step when that pays. No cap per market (owner,
2026-09-26 "No cap — the math decides": the first plan had put four Tier 2
and five Tier 3 markets at exactly the $100 a 10% cap of mine allowed,
with money still worth more there). Per tier it runs to the whole $1,000, which
gives three answers at once: each tier alone with $1,000; the design's
split (each tier's share = its value alone over the sum); and the joint
best split (the tiers' slices merged by value a dollar).

Paper spots: one per side at the side's best price, followed for an hour.
Filled when the other side reaches its price, a trade prints through it,
or a trade prints at its price with nothing left ahead of it in line
(anything that leaves the level may have been ahead). Tier 4's stream
carries full books without trade prints, so there only the first counts
and its fill count is a floor. Tier 3 left the tender's board on 09-24
and with it its stream seats; its 54 markets now ride the Tier 4 monitor
stream so they have live books.

Graded: fills predicted against fills seen per tier (P28); the loss a
share an hour after a fill; and, for our real orders, what the exchange
paid against the meter's estimate per tier since the tier programs began
on 09-24 (the same arithmetic the reward uses).

Not in stage 2: levels that flicker counting for less, and our own real
fills grading the paper spots — both stage 3.

## Stage 3 — paper trading, read-only (owner, 2026-09-26 "We need to go
back to building the bigger 4 tier earning machine")
An engine per tier, deciding every second (Tier 4 every ten) what it
would rest, and keeping the results as if it had:
- Money: the $1,000 split once an ET day at midnight (and at the first
  plan after a boot), each tier's share its value alone (stage 2) over
  the sum. A tier spends its share less what its paper positions hold.
- Values: stage 2's arithmetic on a TIME-AVERAGED book — others' size at
  each price averaged over five minutes, so a level that comes and goes
  counts for the time it is there, as company and as competition for the
  reward. The live book decides where an order can rest (never at or
  through the other side) and when a paper order fills.
- Every 10 s (Tier 4 every minute) the tier's money is spread across its
  markets by stage 2's greedy; between spreads a side whose book moved is
  re-planned on its own share of the money (at most 12 a second).
- A side changes only when the new orders beat the old by more than the
  move costs: the gain a day over how long that side's orders have
  measured to stay best (10 minutes until measured), against the places
  and cancels it takes at an action's price. Actions are capped at 30 a
  minute a tier; when the cap binds, the best move it had to skip sets
  the action's price for the next minute. A side the spread no longer
  funds is given up when its money is wanted elsewhere, weakest first.
- Fills come off the real tape by stage 2's rule. One exit per paper
  position, sized to what is held, at the price whose reward plus gain
  against the fair is best; a position's exit side takes no entry, so the
  lot is never offered twice. Exits go first in the action budget.
- The books a day: reward claimed (on the averaged book), capital tied
  up (his 0.5% a day), realized fills, and the positions marked to the
  midpoint — against what stage 2 predicted for the tier's money over
  the same hours. Each ET day is written down at midnight.
The check on the paper fills: every real order of the tender's gets a
paper twin at its price, filled by the same tape rule; when the real
order leaves the book (ten minutes' grace for its fill to be booked) or
has rested six hours, the twin is scored — both filled, only the twin,
only the real order, neither. That is the measure of how far the paper
fills run from real ones, while nothing real is placed.
Not in stage 3: switches (nothing to switch — it places nothing), the
hand-over of markets from the tender and the old engine (stage 4).

## Build stages — nothing trades until stage 4
1. Fair, read-only: shown per market, logged, graded against where the
   price went (does it beat the plain midpoint?).
2. Value, read-only: what every spot would earn and cost, against what
   the exchange actually pays.
3. Paper trading: the engine decides every second what it would do;
   decisions and paper results logged, nothing placed.
4. Live, tier by tier, each switch off at the start.
