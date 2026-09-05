# Predictions

Owner, 2026-08-23: "We want verifiable and testable predictions and we
want to keep getting closer to the goal of stable and high earnings."

Every claim here is falsifiable, dated, and resolved against the
exchange's own data — `data/rewards.csv` for payouts, `data/trades.csv`
for executions. A prediction that was wrong stays on the page with what
it taught. Nothing is quietly deleted.

State when these were made: politics est $192/day (meter $267/day),
cfb est $228/day (meter $69/day), NBA est $0.30/day (meter $0.08/day).

---

## OPEN

### P1 — Aug-21 pays at least its estimate
**Claim:** when Aug-21 posts, paid ≥ $295.90.
**Why:** the estimate accrues only while books are fresh, and 17.7 of
that day's 24 hours went unmeasured. The book rested through the gap
and the exchange pays for resting, not for our measuring.
**Falsified if:** paid < $295.90.
**Resolves:** when Aug-21 posts (already 3 days late).

### P2 — cfb's estimate is inflated by the own-the-side claim
**Claim:** cfb's Aug-23 payout comes in under $100, against a claimed
$228/day.
**Why:** $232.65/day of cfb's $261.43/day claim comes from 116 orders
that each own >50% of their side. Owning a side only pays if the pool
is real; the independent meter says $69/day, a 3.3x gap. One of the two
is wrong and the payout decides which.
**Falsified if:** cfb Aug-23 pays ≥ $100.
**Resolves:** when Aug-23 posts.

### P3 — the fair cap stops junk buys
**Status 2026-08-23 20:25Z:** 2 breaches already, both explained and
one now fixed. cfb 0 of 130 fills past fair; NBA 0 of 1; politics 2 of
171 after the cap deployed — jdvan BUY 1@57c against a fair the owner
had *just* set (the sweep had not reached the order; fixed by the
priority sweep), and ne-dem BUY 1@8c on a 4.2c fair. Watch whether the
count reaches zero now that owner-set fairs act promptly.

**Claim:** from Aug-24, zero earn fills on MODELED markets land past
fair (conc > 0).
**Why:** the symmetric hard cap refuses those prices outright.
**Falsified if:** any modeled-market earn fill shows conc > 0.005.
**Resolves:** daily from the fills journal.
**Note:** unmodeled markets are excluded — they have no fair line, which
is exactly why the owner-set fair control exists.

### P4 — NBA earns almost nothing for its capital
**Claim:** NBA pays under $1.00 on Aug-24 against ~$47 of capital.
**Why:** joining 400-900k share walls buys a ~0.02% slice of each pool.
This is the accepted price of the stability the owner asked for; the
number is written down so the trade-off is judged on data.
**Falsified if:** NBA pays ≥ $1.00.
**Resolves:** when Aug-24 posts.

### P5 — the exchange will name why orders vanish — **WRONG**
**Claimed:** `cancel_reason` would explain the silent cancels.
**Settled 2026-08-24:** all 2,626 rows read
`UNSOLICITED_CXL_REASON_UNDEFINED`. The field exists and is useless
here, for a reason I should have seen before predicting: the activity
feed carries TRADES. An order cancelled without trading never
produces a trade row, so the trade feed structurally cannot explain a
silent cancel. The field only ever describes orders that DID trade.
**Taught:** check that a data source can contain the answer before
predicting it will. The next probe is the ORDER endpoint, not the
activity feed — the order object carries `state`, so a query over
orders (not activities) is where a cancelled order's fate lives.

---

### P6 — resting is paid twice, taking is taxed  *(new, 2026-08-24)*
**Claim:** over the next week the maker rebate stays near -47 bps of
passive notional and taker fees near +166 bps, so the EV model
understates resting and understates the cost of a dump.
**Why:** measured over 2,626 executions and $7,250 of notional since
Aug-14 — passive trades earned $30.01, the 63 taker dumps paid $14.33.
**Falsified if:** either rate moves more than 20 bps from those.
**Resolves:** Aug-31, from data/trades.csv.

## RESOLVED

### R1 — "the walls are what score" (2026-08-21) — WRONG, and disproven
A group chat claimed the exchange scores from declared best bid/ask
rather than the raw touch. Checked against the reward docs and 185
live markets on the Lite feed: 0 divergences. No change made.

### R2 — "the Texas 50-share exit filled and we lost the record"
(2026-08-23) — WRONG. The transaction history shows no such sale; the
order was cancelled. Taught: infer nothing about fills that the
exchange's own record can settle. This is why data/trades.csv exists.

## P7 — the owner's replacement orders will stop being sold over
*Written 2026-08-24, before the fix ships.*

**Claim.** After the adoption change, no market will ever again show
BOTH an owner order reducing a position AND an engine `sell` order,
where the two together offer more shares than the position holds.

**Why.** `api.open_orders()` sets `manual=True` from the exchange's
`manualOrderIndicator`. `Family.adoptable()` skipped exactly those
orders, so they never entered `self.orders` — and the cover math in
`_sell()` counts only orders it can see (`purpose in ("sell",
"manual")`). A hand-placed replacement was therefore invisible: the
engine read the position as bare and rested its own exit on top.
Observed 2026-08-24 in `brisho` — owner cancelled the engine's
SELL 120 @ 5.5c, placed his own, and the engine re-placed
SELL 120 @ 5.46067c, with two more of the same in limbo. Recording
the order (still `purpose="manual"`, still never touched) closes it.

**Falsifier.** Any single market on /map showing a manual reduce-side
order plus an engine `sell` order whose quantities sum above the held
size. One instance falsifies this outright.

**Check.** Read the order list per market for the next seven days.

---

# Graded 2026-08-24

## P1 — WRONG, and by a lot
Predicted Aug-21 would pay **at least $295.90**. It paid **$93.02**
(politics $76.45, college football $16.57). Off by 3.2x.

**What it taught.** I was reading "no PAID rows yet" as "the exchange
has not told us." It had. Aug-20, 21 and 22 have been sitting in
rewards.csv as PENDING rows the whole time — 387, 175 and 238 of them.
PENDING is not "unknown", it is the number, waiting to settle. Aug-20
read $143.85 on Aug-22 and $143.92 today: seven cents in two days.
Pending totals are final for grading purposes.

I spent four days telling the owner these days were "unposted and
blocking grading" when the answer was in a file I write myself. This
is the second time the same mistake has cost real information — the
first was reading five of the exchange's twenty-four order fields.
**Check the file before reporting an absence.**

## P2 — RIGHT
Predicted college football would stay under $100 all-time. It is at
**$99.11** over three paying days. Right, but by $0.89 — call it
provisionally right and re-grade at the end of the week.

## P4 — RIGHT
Predicted NBA would pay under $1/day. NBA has paid **$0.01, once**,
against 121 resting orders and $48.08 of the $50 cap committed.

## The finding that matters more than any of these
The politics estimate is not slightly high, it is **~3.4x** high:

| day | politics estimated | politics paid | ratio |
|-----|-------------------|---------------|-------|
| Aug-20 | — | $110.19 | |
| Aug-21 | $255.22 | $76.45 | 3.3x |
| Aug-22 | $366.17 | $101.14 | 3.6x |

College football over the same window: estimated $47.66, paid $54.33 —
accurate to 14%. So this is not a broken estimator, it is something
specific to politics.

And the direction is worse than the level. Politics paid $181.52 on
Aug-18, $122.97, $110.19, $76.45, $101.14 — roughly halved in five
days — while orders grew and risk went to the $250 cap. **More money
resting, more orders, less earned.**

## P8 — the politics estimate is wrong because the pool is shared
**Claim.** The politics over-estimate comes from the pool divisor, not
from uptime or spread. Markets in the same race share one pool; the
estimator credits each bracket its own share, so a race we hold five
brackets of is counted about five times.

**Why this fits.** College football markets are one-per-pool and its
estimate is accurate. Politics 2028 nomination markets are dozens of
candidates against a single pool, and they are exactly where the
estimate is largest. Aug 20-22: 225 politics markets paid anything,
only 88 paid more than 50c.

**Falsifier.** Group the Aug 20-22 paid rows by race prefix and
compare each race's total against the sum of its brackets' estimates.
If the gap is flat across races rather than proportional to how many
brackets we hold in each, the divisor is not the cause and this is
wrong.

**Check.** Runnable now against rewards.csv and estimates.csv — no
waiting on the exchange.

### P8 — first check, and an admission
Pay per bracket does not collapse as we hold more brackets of a race,
which is what pool-splitting would look like:

| brackets held | races | paid per bracket |
|---------------|-------|------------------|
| 1 | 56 | $0.60 |
| 2 | 39 | $0.97 |
| 3 | 3 | $1.81 |

And the widest: 23 brackets of `ewc-usp-2028-11-07` paid $1.80 each,
12 of `enwc-uspres-nom-rep-2028` paid $3.50 each. Wide races pay MORE
per bracket, not less. If one pool were being counted once per
bracket, this column would fall like 1/N. It rises.

**But that is not the falsifier I wrote.** The stated test needs each
race's ESTIMATE, and estimates.csv only stores a per-family total. I
cannot grade a market or a race against its own prediction, which
means P8 is undecided rather than refuted — and the reason is a hole
in my own record-keeping, not in the exchange's.

The owner asked on 2026-08-23 that estimates stay written down until
the actual numbers come in. I wrote them down per family. Per market
is what makes them testable. That is the next thing to build.

---

## 2026-08-24, later — I was wrong about what we have

The owner asked whether we have per-market estimates or fill data at
all. We have both, and I had just said otherwise.

* **data/fills.csv — 633 fills.** Each one carries the market's
  estimated daily rate AT THE MOMENT WE WERE RESTING THERE, plus our
  fair, the band, the confidence, both sides of the touch, hours
  rested and the order id.
* **data/trades.csv — 2,739 trades**, with realized P/L, placement
  time, hours rested, commission, maker/taker role.
* **data/rewards.csv — 4,108 market-day payout rows** over 51 days.

So the join I said was impossible runs today. 73 market-days have
both our estimate and the exchange's money:

| | market-days | estimated | paid | ratio |
|---|---|---|---|---|
| politics | 55 | $172.56 | $58.18 | 2.97x |
| college football | 18 | $31.27 | $6.59 | 4.74x |
| **all** | **73** | **$203.83** | **$64.77** | **3.15x** |

**Read this carefully — the sample is biased.** fills.csv only
records markets where we were FILLED, which are the markets that
moved. So the honest claim is narrow and still useful: *on the
markets where we got filled, our estimate was about 3x the money.*
Getting filled and over-estimating travel together.

Note college football is 4.74x over here while its FAMILY estimate
for Aug-22 was accurate to 14% ($47.66 est, $54.33 paid). Both are
true — of different samples. The family number averages over the
quiet markets that pay as predicted; this one is the filled tail.

**The actual gap** is narrower than I claimed: we have no stored
estimate for markets we rested in and were NOT filled — the good
ones. data/market_est.csv, deployed today, records exactly those.

## P9 — realized trading P/L is about -$31, not -$3,110
`realized_pnl` in the activity feed is in **cents**, not dollars.

Four trades at one instant on 2026-08-17 in
`ewc-usp-party-2028-11-07-rep` (37, 128, 2 and 183 shares at 39c)
report -108.93, -376.84, -5.89 and -538.75 — exactly -2.944 per
share each. At $2.94/share that is impossible on a 39c contract; at
2.9c/share it is ordinary. Confirmed independently: 400 bought at
41c on Aug-15, 50 sold at 28c the next day, field says -6.37 on 50
= 12.74c/share against a 13c predicted move.

**So: -$31.11 of realized trading loss across 2,739 trades, against
$6,118.30 of rewards paid.** The trading side is roughly flat and
the rewards are close to net profit. I was one report away from
telling the owner we had lost $3,110.

**Unexplained, flagged not resolved.** `scc-hrep-rep-2026-11-03-gte200`
contributes -$1.64 with per-share numbers that need a cost basis
above 100c (bought 1 at 67c, sold 1 at 68c, reported -55.91c). That
only makes sense if the shares arrived through a negative-risk
bracket conversion rather than a purchase. One market, 5% of the
file's total. Do not average it in without understanding it.

**Falsifier for P9.** Any trade whose realized_pnl/shares falls
outside +/-100. All 659 non-zero rows are inside it today.

---

## 2026-08-24 — what the "Transfer" rows are

The owner asked about a **+$14.60 Transfer, completed between 12 and
1pm ET today**, and said several others arrived a few days ago.

**A Transfer is liquidity reward money landing as cash.** Not a
separate program. Rewards are posted per market-day as PENDING, then
settle to PAID, and the cash arrives in the account as a Transfer.

This one is exact to the cent. Aug-19 still had $14.60 pending in
four rows at the 10:56Z snapshot today, and all four are
`paccc-balpow` — the **balance of power markets, which the owner
trades by hand** ("Don't place any orders in the balance of power.
I'm going to do that one by hand", 2026-08-22):

| | |
|---|---|
| paccc-balpow-2026-11-03-dhou-rsen | $7.59 |
| paccc-balpow-2026-11-03-rhou-dsen | $3.37 |
| paccc-balpow-2026-11-03-rsweep | $3.26 |
| paccc-balpow-2026-11-03-dsweep | $0.38 |
| **total** | **$14.60** |

So that payment is his own book, not the engine's. His hand-placed
balance-of-power orders earned $14.60 on Aug-19 and $2.84 on Aug-20.

The earlier ones were the daily payouts: Aug-14 $274.92, Aug-15
$1,352.63, Aug-16 $197.03, Aug-17 $295.29, Aug-18 $181.52, Aug-19
$108.37.

### Correction: pending totals are NOT final
Earlier today I wrote that "pending totals are final for grading
purposes" on the strength of Aug-20 moving seven cents in two days.
That was too strong, and today's own snapshots refute it: Aug-21
read **$80.38 at 10:56Z and $93.02 by 16:00Z** — $12.64 added in
five hours.

Older days settle; recent days are still filling in. Aug-20 was
stable because it is old, not because pending is final. **P1 stays
graded wrong** — $93 against a $295.90 prediction does not close
from accretion — but the exact figure is not yet fixed, and no
recent day should be treated as final.

A day also does not settle all at once. Aug-19's $108.37 settled
before today while these four rows settled at midday today, five
days after they were earned.

## P10 — the pending/paid/Transfer chain
**Claim.** Every dollar in rewards.csv that flips PENDING -> PAID
appears as a Transfer of the same amount in the account within a
day, and no Transfer arrives that does not correspond to such a
flip.

**Falsifier.** A Transfer whose amount matches no set of rows
flipping to PAID, or a flip to PAID with no Transfer following it.

**Check.** Aug-20's $143.92, Aug-21's (currently $93.02) and
Aug-22's $155.47 are all still pending. Each should arrive as one
or more Transfers. Watch the amounts against the per-market rows.

---

## 2026-08-24 — markets that used to pay and now pay nothing

The owner asked. Comparing Aug 14-18 (the strong stretch) against
Aug 20-22 (now): **105 markets paid then and pay nothing now, worth
$149.07/day.** That is the entire decline and more.

| why it stopped | markets | $/day |
|---|---|---|
| **still open — we simply stopped earning** | **77** | **$104.87** |
| on the owner's avoid list (deliberate) | 19 | $26.10 |
| out of scope — WNBA | 2 | $12.59 |
| market resolved before Aug-20 | 7 | $5.51 |

### Wrong theory, recorded
I expected getting FILLED to be what kills a market — take a
position, the book turns exit-only, the rewards stop. The single
biggest loss fits it perfectly: `ewc-usp-party-2028-11-07-rep` paid
$79.85 on Aug-15, we bought 400 shares at 41c that evening and 350
more at 39c on Aug-17 (position +695), and it paid $0.17 on Aug-18
and nothing since.

**It does not generalise.** Markets that KEPT paying had a higher
fill rate than those that stopped — 91% (126/138) against 61%
(47/77). Fills are normal everywhere. One market's story is not a
mechanism.

### What does explain a large piece of it
**10 of the 77 match none of `enter_tokens`, and they are worth
$38.68/day** — including the top two losses:

| $/day | market | |
|---|---|---|
| $22.10 | `ewc-usp-party-2028-11-07-rep` | which party wins 2028 |
| $9.79 | `apdc-jerpowgov-2026-12-31` | Powell departs as Fed governor |

`enter_tokens` is `("usgub","usse","senate","uspres","usp-2028",
"usho","scc-hrep")`. The party market's slug is `usp-party-2028`,
so `usp-2028` does not match it and neither does `uspres`. **Our
single best market by far is excluded from fresh money by a hyphen.**

Across all history, non-sport markets that match no enter token
have paid **$1,079.61 — 18% of the $6,118.30 we have ever earned.**
The biggest excluded families are `apdc-*` ($346.24, "will official
X depart") and `ewc-*` ($260.79).

### Still unexplained
The other **67 markets, $66.18/day**, DO match an enter token and
stopped anyway. Candidates are the 50c/day entry bar with its
two-reading cull, and capital exhaustion at the $250 politics cap.
I do not know which, and I am not guessing: data/market_est.csv
records every market we rest in with its estimate whether or not it
fills, which is exactly the missing evidence. Two days of it will
answer this.

---

# 2026-08-24 — hunting the estimator's wrong input

Owner: "We have to get better at estimating in real time."

The estimator refuses correction factors by design — "wrong output
means a wrong input." So: find the input.

### Ruled out 1: the pool divisor. I was wrong about this.
The probe file shows only 6 distinct `programId`s across 162
markets — `politics_high_20260727` is $300/day against 62 markets —
and our divisor (the race group, typically 2-20) is smaller than
that programId group for 160 of 162. I concluded we were dividing
by too little and the estimate was therefore ~4.4x too big, which
matched the measured 3.3-3.6x almost exactly.

**It is still wrong.** `period` reads `daily_event`: the pool is
per event per day, and the programId is a TIER label, not a pool
group. Several events each get $300/day. The arithmetic falsifies
my version outright — under it the whole politics board would offer
$425/day, and we were PAID $1,352.63 on Aug-15.

v3/programs.py already carries this, settled on the 2026-08-15
payout, with "do not re-open this" written next to it. I re-opened
it, got a number that matched the symptom, and nearly shipped it.
**A wrong theory that predicts the right magnitude is the most
dangerous kind.** Check it against a number it must also explain.

### Ruled out 2: stale books.
Politics measured 23.5 of 24 hours on Aug-21 and 22.8 on Aug-22.
Nothing is being extrapolated across a dead feed. (College football
is a different story — 17.2h and 19.3h UNMEASURED — which is why
its estimate reads LOW and lands close: $47.66 estimated, $54.33
paid, from about five measured hours.)

### What the error actually tracks
73 market-days with both our estimate and the money:

| estimate size | estimated | paid | error |
|---|---|---|---|
| smallest third | $14.89 | $17.51 | **0.85x — slightly LOW** |
| middle third | $54.35 | $19.96 | 2.72x high |
| largest third | $134.59 | $27.30 | **4.93x high** |

And by book width at the time:

| | median spread | error |
|---|---|---|
| tight books | 2.0c | 2.26x high |
| wide books | 10.0c | 4.17x high |

**The estimator is accurate when it predicts a little and wildly
optimistic when it predicts a lot.** Never once did it predict
money and get nothing — 0 of 73 — so the qualification logic is
sound. It is the SIZE of the claim that inflates.

Both cuts point one way: our share is read off a single snapshot
and billed as if it held all day. In a thin or wide book our size
looks dominant at the instant we look, and does not stay dominant.
The bigger the share we think we have, the further it has to fall.

## P11 — the estimate's error is share-persistence, not pool size
**Claim.** Our realized share of a side is systematically lower
than the snapshot share, by more the higher the snapshot share is.
A market whose snapshot share is under ~10% will pay close to
estimate; one over ~40% will pay a third of it or less.

**Falsifier.** Bucket market-days by snapshot share and compare
paid/estimate. If the error is flat across buckets, share
persistence is not the mechanism and something else inflates large
estimates.

**Check.** data/market_est.csv began recording every market we rest
in today, filled or not, which is the sample this needs. Two days.

---

# 2026-08-24 evening — politics only

## The exchange's own formula, from its documentation
`Score = DiscountFactor ^ (ticks from best price) x OrderSize`, each
side scored independently, **each side normalized to 1.0 per snapshot
provided Target Size is met on that side**, every second of the period
weighted equally, payout pro-rata by share of total score.

Our arithmetic is the same formula. So the error is in an input or in
how we compare, not in the shape of the maths.

## The measurement that matters
Using the probe file's real pools and the exchange's own payouts,
881 politics market-days:

| share of ONE side's daily pool | engine claims | exchange pays |
|---|---|---|
| 25th percentile | 4.0% | 2.0% |
| **median** | **49.5%** | **9.5%** |
| 75th percentile | 70.8% | 23.9% |
| 90th percentile | **100.0%** | 48.6% |

The engine's 90th percentile is 100.0% — it routinely believes it owns
an entire side of a book. Its median belief is half of every book it
rests in. On a real exchange with other market makers, that is not
credible on its face, and it is the single most useful number found
today.

Also: 58 market-days paid MORE than one side's whole pool (max 482%),
which says the pool model errs LOW in places — matching the note in
programs.py that per-event division "erred low" when it was settled.

## Two theories killed today, both mine
1. **Target Size truncating the denominator.** We do use Target Size
   twice — as the side's gate (correct, matches the docs) and again to
   cut the denominator at `window_levels`. The docs say it is only the
   gate. But measured, the truncation is worth **1.0-1.1x**, not 5x.
2. **The feed handing us shallow books.** 370 stored snapshots cap at
   4-5 levels a side, which looked damning. Measured with size per
   level held constant: a ladder seen 3 deep scores **37.5%**, the same
   ladder 20 deep scores **36.8%**. `df**ticks` decays faster than
   depth accumulates, so past the fourth level a maker is weightless.

   I first "measured" this as 37% against 10.7% — but that comparison
   changed the size per level from 400 to 2000 as well as the depth.
   The SIZE was doing all the work. My own unit test caught it.

**Both are now pinned as tests** so neither gets re-proposed.

## What that leaves, stated honestly
Our share is far too high, and neither depth nor the window explains
it. What moves the share is SIZE AT AND NEAR THE TOUCH: ten times the
size one tick behind us cuts our share by more than three.

And one flaw in my own comparison, which has to be resolved before
anything is concluded: `est_day` is a RATE per day, while `paid` is a
whole day's money. If our orders rest only part of the day, the two
are not comparable, and roughly 20% uptime alone would turn 49.5%
into 9.5%. The estimator does integrate uptime correctly for the
family total — which is why the family is 3.4x high and not 5x — so
this cannot be the whole story, but it is certainly part of the
measured gap.

## P12 — it is size near the touch, not depth or uptime
**Claim.** With uptime held constant, our computed share will still
exceed realized share by 2x or more, and the gap will track how much
size sits within two ticks of the best price.

**Falsifier.** If computed share x (live_h / 24) matches realized
share within 20% across politics markets, the arithmetic is right and
the whole "3.4x" was uptime plus my bad comparison.

**Check.** data/market_est.csv now carries share, live_h, realized
share, AND book depth per market, all measured over the same seconds.
This is answerable from one full day of it — no theory required.

---

## 2026-08-24 20:45Z — the hand-order fix is confirmed live

Build e29e7309. The owner's own orders are now recorded, and the
engine sizes around them instead of over them:

| market | his order | engine's order |
|---|---|---|
| MA micmin (short 335) | BUY 334.84 @ 98c | BUY **0.16** @ 92.9c |
| NH chrpap (short 180) | BUY 179.9 @ 96c | BUY **0.10** @ 95c |
| brisho (long 120) | SELL 120.02 @ 2c | **none** |

Before the fix the engine re-placed SELL 120 @ 5.46c over him in
brisho every time he cleared it. It now rests nothing there. The
leftovers are the fractional remainders his orders do not cover.
Politics manual orders read 137, up from 48 — that is the fix
recording what it used to discard, not new orders appearing.

## Aug-20, 21 and 22 all settled to PAID today

| day | politics estimated | paid | off by |
|---|---|---|---|
| Aug-21 | $255.22 | $76.45 | 3.3x |
| Aug-22 | $366.17 | $101.14 | 3.6x |

(Aug-20's $6.23 estimate is a partial-day artifact — ignore it.)
**The 3x is now confirmed on final numbers, not pending ones.**

## Retracted: "the engine thinks it owns half the book"
That came from dividing what we were paid by MY OWN guess at the
pool, where I counted markets-per-race across only the 156 markets
in the probe file. The engine uses the exchange's events feed and
knows better than my guess did.

Using the engine's own recorded numbers instead — its time-weighted
share and the pool it actually competed against — today's median
politics share is **1.4%**, not 49.5%. The whole "engine claims half
of every book" finding was an artifact of my arithmetic. It is dead.

Against Aug-22's realized share (median 5.4%), today's computed
share runs a median 0.30x — the engine reading LOW, not high, with
an enormous spread (25th 0.01x, 75th 3.27x).

**That comparison is not clean** and must not be treated as a
result: it puts today's share beside a different day's payout. The
honest test is today's share against today's money, which settles
around Aug-29. Four theories have died today by being checked; this
one is not getting announced before it is.

---

## R3 — the Texas cover test — SETTLED 2026-08-24, and it PASSED

**The trade.** `usgubewc-usgub-tx-2026-11-03-rep`, Aug-21 19:58: an
exit, BUY 1 @ 91c against an 87.5c model, book 87c/92c. It rested
8.1 hours before filling. Cost: 3.5c past model value = **-$0.04**.
The card claimed **~$1.06** of rewards earned while it rested, from
an estimated $3.12/day.

**The owner's test, in his words:** "If our numbers are right this
will be +ev and a great call. Otherwise it could be a small loss.
We just have to check tomorrow."

**Settled.** Aug-21 posted PAID. That market paid **$1.32** for the
whole of Aug-21 — every order we had there, both sides, all 24
hours. Our order held 8.1 of those hours, so it earned **at most
$0.45**, and less if anything else of ours rested alongside it.

| | |
|---|---|
| cost | -$0.04 |
| earned in 8.1h | about +$0.45 |
| **net** | **+$0.41** |

**The call was right.** Covering there was +EV by roughly ten times
what it cost. Paying 3.5c past model to hold a spot that pays was
the correct trade.

**And the number that justified it was still 2.4x too big.** The
claim was $1.06 for those 8.1 hours; the market's own payout caps it
at $0.45. That is the same overstatement seen everywhere in politics
this week (3.3x on Aug-21, 3.6x on Aug-22, 3.4x across the Texas
markets as a group).

**What this settles that nothing else did.** The estimate is
INFLATED, not INVENTED. The money is real, the direction is right,
and a decision made on these numbers can still be correct — it just
has less margin than the card says. A 2.4x-optimistic estimate is
safe on a trade that wins 10x its cost and dangerous on one that
looks marginal. Until the share measurement lands, treat any
decision whose claimed edge is under ~3x as unproven.

**Written down late, which is the lesson.** This test was agreed in
conversation and never entered this file, so when the owner asked
whether it had come back I could not tell him what it was. The rule
exists for exactly this. Anything we agree to check goes in here
first, before the data lands.

---

## 2026-08-25 — RETRACTED: "the exchange re-tiered politics"

I told the owner the exchange had moved most politics markets into a
cheap $25/day pool whose discount factor (0.10) pays a tenth one tick
off the touch, and that this explained the collapse. He asked for
evidence that it was a CHANGE. There is none.

**What I did wrong.** I compared the Aug-18 probe file (162 markets,
whatever I happened to probe) against the live terms (549 markets)
and read the difference in counts — "70 in the cheap tier then, 343
now" — as the exchange moving markets. It is a sample against a full
set. The counts differ because the sets differ.

**The correct test**, on the 156 markets present in BOTH snapshots:

| | |
|---|---|
| moved into the cheap df-0.10 tier | **0** |
| discount factor made worse | **0** |
| `politics_high` -> `presidential_election_2028`, $300 -> $200, df 0.20 both | 60 |
| `politics_mid` -> `politics_gov_race_20260817`, $100 -> $20 | 1 |

Nothing was demoted. No discount factor changed anywhere. The cheap
tier's 0.10 was already there on Aug-18 — it is not new.

The one real change is 60 presidential markets getting a dedicated
$200/day program instead of a share of the $300 pool. That is a cut,
but it is 60 markets and one pool, and it cannot account for politics
falling from $295.29 (Aug-17) to $36.05 (Aug-23).

**The proposed engine change is withdrawn** — I had asked to stop
quoting behind the touch in cheap politics markets. Its premise was
this retracted finding. It may still be a good idea on its own
merits, but it will not be proposed again off this evidence.

**Still unexplained, and now with no live theory:** politics down 8x
in six days, markets paying 162 -> 84, estimate error growing 3.3x ->
3.6x -> 7.1x.

**The lesson, which is the owner's:** "You can't just say things."
Five theories dead in two days. Four I killed by checking my own
arithmetic; this one survived until he asked for evidence I had never
gathered. Before the next claim: name the two things being compared
and confirm they are the same population.

---

## P13 — the diminishing-capacity mechanism was junk intake
*Written 2026-08-25 evening, all of the day's rules live (41327fd).*

**The owner's diagnosis, confirmed as today's whole arc:** every bad
fill converts earning capital into stuck inventory. The dollar keeps
counting against the cap but stops earning, so quoting capacity
shrinks while risk stays maxed — politics fell $295.29 -> $36.05
over six days at a pinned $250 cap.

**Claim.** With the intake rules live (deflated bar, speed-graded
learning, probe-only fronting, the evidence cap, the sweep, the
nurse) and the exit gate working the other end:
1. Politics' junk intake stops: across the next 7 days, no new
   politics position opens more than 3c past the evidence band or
   model fair at entry.
2. Politics' daily paid stops falling and recovers to at least
   $75/day by Aug-31 as capital frees, without the cap being raised.

**Falsifier.** A new kamhar-class fill (entry past value on an
informed model-less market), or politics paid still under $50/day on
Aug-31 with the rules live and quoting volume healthy. If quoting
collapsed instead (the owner's "barely quoting" trigger), the
deflator is re-examined first — that failure mode is over-tight
intake, not junk.

**Check.** data/fills.csv entries vs band/fair per fill;
data/rewards.csv daily politics totals; quoting volume from state.

---

## P14 — where the claims live and where the money lives are different market classes
*Written 2026-08-26 ~02:30Z, BEFORE the Aug-25 politics postings land.
From the first full day of the per-market ledger (Aug-25, 151 politics
markets) joined against the posted-pay history through Aug-24.*

**The pattern found.** Three facts from the data, one story:

1. **$246.89 of the day's $381.35 raw politics claims (65%) sit on
   markets whose trailing pay is ~zero** — led by safe-seat winner
   books (AL/NE/AR/OR governor REP, MS senate REP) claiming a uniform
   ~$6.25/day each. On those rows the ledger's own two estimators
   disagree: the order-path est says $6.25 while the sampler's own
   share x pool says $1.6–2.6 — a ~2–4x divisor-shaped gap on
   REP/DEM paired listings of one race. Exchange trailing pay on
   them: pennies.
2. **On the markets that actually pay, the order-path est reads
   $0.00.** Senate-control DEM: sampler share 1.000 of a $75/day
   pool, order est $0.00 ("side below Target Size"), exchange paid
   $2.29–2.71 every posted day. Buttigieg nominee: order est $0.00,
   paid $1.47–2.71 daily. The engine's qualification read calls the
   paying markets worthless and the worthless markets rich.
3. **Membership in the paid set churns**: 45 politics markets paid
   >5c on Aug-23, 33 on Aug-24, only 14 on both — though Aug-24's
   posting arrived 26h late and may still be filling in, so churn is
   provisional.

**Claims (falsifiable when Aug-25 politics posts).**
1. The $246.89 claimed on zero-trailing markets collects **under $10
   total**.
2. The order-est-zero payers keep paying: senate-control DEM and
   Buttigieg nominee each post **>= $1** for Aug-25.
3. Politics Aug-25 total lands **$25–70** — near trailing reality,
   nowhere near the raw $381 or even the deflated $127.

**Falsifiers.** (1) fails if the safe-seat books collect real money —
then the empty-book arithmetic is right and the divisor theory wrong.
(2) fails if the big-pool payers post ~$0 — then the Target-Size read
is right and the exchange's own postings were the anomaly. (3) failing
high means the claims ledger is better calibrated than three days of
history suggest.

**Check.** data/rewards.csv day 2026-08-24/25 politics rows vs
data/market_est.csv day 2026-08-25 (frozen).

### P14 — GRADED 2026-08-27, all three claims WRONG, each one instructive
*Aug-25 politics posted $119.79 across 203 markets (~26h after day end;
postings can still grow in waves — graded on what stands at 02:30Z).*

1. **"Zero-trailing claims collect under $10" — WRONG.** The 92
   markets with ~zero pay across Aug-22–24 collected **$53.78** — 45%
   of the day's politics money came from markets that looked dead
   three days running. Lesson: the exchange's paying set CHURNS;
   trailing pay is a bad predictor of tomorrow's pay, and the
   divisor theory as stated dies here.
2. **"The reliable payers keep paying" — WRONG.** Senate-control DEM
   paid $0.03 (from $2.29–2.71 daily); Buttigieg $0.33. Even the
   always-pays class rotates. The order path's Target-Size zero-read
   on them was not obviously the anomaly — they genuinely stopped.
3. **"Politics lands $25–70" — WRONG, HIGH: $119.79.** Paid trend
   $101 → $36 → $24 → $119.79 — the collapse reversed on the first
   full day of the complete rule set. P13's "$75/day by Aug-31" was
   met on Aug-25, four days early; one day is not a trend.

**What survived:** the family-level deflator. Joined markets claimed
$348.13 and were paid $98.23 (~3.5x); claims ÷ 3 ≈ $116 against
$119.79 actually paid — the ÷3 deflator is empirically almost exactly
right where per-market prediction is hopeless. Predict the family,
not the market.

### P15 — written 2026-08-28 ~12:30Z, BEFORE the Aug-27/28 postings land
*Context: Aug-26 posted $389.41 — the record day ($304.25 politics
across 180 markets, best single market $9; $85.16 cfb). Raw politics
claims metered $117 across only 10.6 hours (804 min unmeasured —
maintenance); scaled to 24h that is ~$265 vs $304 paid, a ~0.9x
ratio. Aug-25's same ratio was ~2.9x. The exchange re-tiered politics
Aug-27 and created the MA MoV boost ($1,000/day/event); the owner's
walls stood from ~03:30Z Aug-28.*

1. **Aug-27 politics posts LOW: $60–$180.** The re-tier cut pools and
   the maintenance wipe erased resting hours; the raw claim rate that
   day metered $62 over ~16.8 hours. Wrong above $180 (the churn
   lesson of P14 repeats) or below $60.
2. **Aug-28: the 11 MoV brackets together post ≥ $50** (vs $2.52 on
   Aug-26, pre-boost) — the walls qualify the sides and the near-touch
   asks capture a real slice of 11 × ~$91/day. Wrong under $20: then
   the boost pays differently than our terms read (the per-event
   divisor, or deep walls counting for less than the window math
   says).
3. **The claims-to-paid ratio stays compressed:** Aug-27 AND Aug-28
   politics claims ÷ paid each land ≤ 1.5x. If both hold, the ÷3
   deflator is suppressing entries roughly 2x too hard and I will
   propose dropping est_deflate toward 1.5 — the owner's yes decides,
   nothing changes on this prediction alone.

**Check.** data/rewards.csv days 27/28 against the frozen claims in
data/market_est.csv; MoV = the 11 vmc-ussep-mov-ma-dem markets.

### P15 claim 1 — GRADED 2026-08-29: WRONG, HIGH again
*Aug-27 posted $251.95 total — the second-best day ever, ON the
maintenance-wipe day: politics $218.90 across 206 markets (best
single market $4.44), cfb $33.05, MoV $1.16 (pre-wall). The $60–180
band broke on the high side.*

Lesson, same as P14's: every "politics goes down because X" call has
been wrong — the re-tier and the wipe together could not hold it
under $180. And the deflator evidence is now two days in one
direction: raw claims ~$88 (24h-scaled) vs $219 paid (~0.4x) after
Aug-26's ~0.9x. The engine decides on claims ÷ 3 — it believed ~$29
while earning $219. Claims 2 and 3 wait on Aug-28's posting.

### P15 claims 2 & 3 — GRADED 2026-08-30: both RIGHT
*Aug-28 posted $755.41 — the record, nearly double Aug-26's record:
politics $560.26 across 194 markets, cfb $195.15 across 205. The
postings arrived in waves at 01:09–01:15Z, ~2 days after day end.*

2. **MoV ≥ $50: RIGHT, four times over.** The 11 brackets paid
   **$199.65** ($10.49–$22.87 each) on their first boosted day with
   the owner's walls standing — against $2.52 pre-boost. The
   qualify-the-side mechanics work exactly as the terms said.
3. **Claims ÷ paid ≤ 1.5x: RIGHT.** Aug-27 ~0.4x, Aug-28 ~0.55x
   (frozen claims $233 metered / ~$309 24h-scaled vs $560 paid).
   Acted on 2026-08-29 with the owner's yes: est_deflate removed.

P15 final: claim 1 wrong (the third straight "politics goes down"
call to break high), claims 2 and 3 right. Bonus grade: the cfb $100
budget bump was estimated at +$40–70/day and delivered +$110
($85.16 → $195.15) on its first full day.

### P16 — written 2026-08-30, BEFORE the weekend/post-primary data
*The politics run-up decomposed: breadth (48 -> ~200 paying markets
since Aug-25, median $1.25, top-20 just 30%), rate per market ($0.60
-> $2.89 avg, re-tier + correct terms + live books), and the MoV
boost + walls ($199.65/day, ends when the MA primary resolves
Tuesday Sep 1).*

1. **The structural base holds:** politics EX-MoV posts ≥ $180/day
   for Aug-29, 30, and 31. Wrong if any day lands under — then the
   record week was tide, not engine.
2. **The cliff is resolution, not failure:** post-primary (Sep 2+),
   MoV pays ~$0 and TOTAL politics still posts ≥ $200/day. Wrong
   under $200 — then the base was leaning on the boost more than
   the decomposition shows.

**Check.** data/rewards.csv days 29-31 and Sep 2-3; MoV = the 11
vmc-ussep-mov-ma-dem markets.

### P17 — written 2026-09-05 ~01:30Z, BEFORE the game-day experiment runs
*The setup: the Game day family (v3/gameday.py) rests ONE share a side
at the touch on seven markets of each of Saturday's two biggest college
games — moneyline, the spread and total nearest 50c, first-half winner,
a first-half spread, a team total, a first-touchdown prop — and pulls
everything at kickoff. About $14 of collateral. The Miami–Stanford probe
(2026-09-04): 295 markets, $42,000 in pools, Target Sizes 100–200k on
the main lines and 10–25k on the small ones, df 0.10–0.30.*

1. **A side under Target Size pays nothing.** Every scout market-day
   whose side rested under the program's Target Size (the prop, and
   any period market whose books were thin) posts $0.00 for us in
   data/rewards.csv, our order at the touch notwithstanding. Wrong if
   any such market-day pays ≥ $0.05 — then the "side must reach Target
   Size" reading of the terms is wrong or incomplete, which would
   matter far more than this experiment.
2. **A share against the walls earns pennies.** The main-line scouts
   (moneyline, spread, total) each post under $0.10 for the day: one
   share beside 30,000–200,000 resting, at df 0.10–0.15. Wrong if any
   main-line market-day pays us ≥ $0.50.
3. **Game-day flow fills them.** At least half the scouts fill before
   kickoff. Wrong if fewer than 7 of 14 fill (data/fills.csv, family
   gameday, purpose earn).
4. **The fills are cheap.** The hour-on grade of those fills (fill_graded
   in the family log) has a median adverse move ≤ 5c a share. Wrong if
   the median exceeds 5c — then day-of markets are not where a resting
   strategy belongs, however large the pools.

**Check.** data/rewards.csv rows for the two game slugs on the game
date; data/fills.csv and data/trades.csv for family gameday; the
kickoff_pull log events for what left and when.

### P18 — written 2026-09-05 ~05:00Z, BEFORE the split placement runs
*The setup (owner: "the placement should be selected so as to have
minimal exposure and yet reach the 60% or 30% threshold"): a bond exit,
or its buy-more, may now rest at TWO levels — a few shares up front
where each counts most for the score, the rest behind where they hardly
sell — when the fill model says that cuts expected fills (exits) or
expected overpay (buy-more) by at least 25% against the one-level
answer. The bonds log records each split with `exposure` (expected
shares sold a day) and `one_level` (the same for the single level it
replaced).*

1. **Splits keep the threshold.** Every split exit's market-day pays at
   least 55% of what the whole lot at the touch would have (the calc
   block's "whole lot at the touch" figure, recorded at the time),
   i.e. the 60% target less measurement noise. Wrong if a split
   market-day pays under 50% of that figure.
2. **Splits sell less.** Over the first week, shares sold from split
   exits per share-day resting are under 75% of the rate from
   single-level exits over the previous week (data/trades.csv, bond
   sales; the bonds log `sold` events). Wrong if the rate is not lower.
3. **The few up front are what sells.** Of the shares sold from split
   exits, at least 80% come from the near level. Wrong if the far level
   sells more than a fifth — then the fill model's depth prior is off
   and the exposure figure with it.
4. **No churn.** Split exits re-lay themselves at most once a day per
   market on average (bonds log events exit_split, exit_unsplit). Wrong
   if it is more than twice a day — then the margin is too thin.

**Check.** The bonds log (exit_split / exit_unsplit / more_split with
their exposure figures); data/trades.csv for bond sales by price level;
data/rewards.csv for the market-days.
