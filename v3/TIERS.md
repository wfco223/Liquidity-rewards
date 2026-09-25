# Tier engines — design draft (2026-09-25)

Nothing here is built yet. Each stage below needs the owner's yes.

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
| T4 | midterms_t4_* (House winners) | 5 | 2,000 | 0.40 | 829 |

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

## Build stages — nothing trades until stage 4
1. Fair, read-only: shown per market, logged, graded against where the
   price went (does it beat the plain midpoint?).
2. Value, read-only: what every spot would earn and cost, against what
   the exchange actually pays.
3. Paper trading: the engine decides every second what it would do;
   decisions and paper results logged, nothing placed.
4. Live, tier by tier, each switch off at the start.
