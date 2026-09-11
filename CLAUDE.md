# Owner preferences — read before doing anything

This file loads automatically in every session.

**Working on 3.0 (the politics-first merge of both versions)? Read
v3/DESIGN.md.** It is short and states what 3.0 keeps from each parent.

**Building version 2.0? Read REBUILD.md next.** It is the brief: what the
system does, what state each part is in, what is actually broken, and the
decisions already made about the rebuild. Start there, not in the code.

Working on the running 1.0? HANDOFF.md has the operational history — it is
long and accreted, so search it rather than reading it through.

## Standing rule (owner, 2026-08-25 — supersedes everything below)
- "Don't make changes any more without checking with me." EVERY code,
  config or deploy change gets the owner's explicit yes first, stated
  plainly enough to approve on a phone. No exceptions for "obvious"
  fixes, reverts of my own work, or diagnostics that touch behavior.
- When the owner corrects a factual claim (e.g. 2026-08-25: the party
  market and the candidate book ARE the same market — corrected twice),
  the correction is the ground truth going forward. Do not re-litigate.

## How to talk to the owner
- Plainly. No characterizing, no hype, no hedging language. Lead with the
  numbers and what happened.
- Verify claims against data before asserting them. If the exchange or a
  file can answer the question, check it first.
- The owner works ENTIRELY from a phone. No command line, no laptop.
  Anything the owner must operate has to work as: tap a link, tap a button,
  or edit a file in the GitHub mobile UI (the poke.txt pattern).

## How the app is built (owner's choices — keep them)
- Two repos. wfco223/Liquidity-rewards (private) holds everything.
  wfco223/welcome is a group-visible fork: NO tracker data, activity,
  balances, or market info ever goes there.
- STATUS.md is the phone-readable front page: one ✅/❌ freshness line up
  top, summary before detail, plain-English explanations of every number.
- The live monitor (live/monitor.py) runs on DigitalOcean from the `deploy`
  branch and only picks up code on restart. The /map page is the owner's
  control surface: tiles by state, per-order Move/Cancel, order book,
  new-order form, and the automation switches.
- Automation switches: NOTHING places orders unless the owner turned that
  loop's switch on from /map. Off by default, persisted in state["auto"],
  every flip audit-logged. Turning ON takes two taps; OFF takes one.
  Never add automation that places orders without such a switch.
- Orders the owner placed by hand are untouchable (owner, 2026-08-22
  "Don't let it cancel orders I set by hand"): the engine never cancels,
  moves, or reprices them — any resting order the engine did not place
  itself is treated as the owner's. It sizes its own exits and dumps
  around them so shares are never offered twice. One carve-out (owner,
  2026-09-07 "If I try to sell something and there is an order in the
  way, ask me if I want to cancel the order and if I say yes, cancel
  the order and then sell"): a sale by his tap that his own order
  blocks comes back as a question naming the order; his yes cancels
  that one order and runs the sale. Never without the yes. The
  other carve-out (owner, 2026-09-10 "my orders should be like any
  other the tender places, susceptible to being moved if there is
  another place they could be resting that is more positive ev"):
  in a focus market he has given a fair, his own orders are the
  focus tender's to move, resize and pull under its rules, his 1c
  and 99c qualifying walls excepted; where no fair is set they stay
  as he left them.
- FROZEN ground — the engine does NOTHING there (owner, 2026-08-24
  "Don't sell my gop governor count race orders. In fact don't touch
  those"): places nothing, rests no exits, reprices nothing, cancels
  nothing. Whatever is resting stays exactly as it is. This is
  stricter than the avoid list, which PULLS the engine's orders out.
  Currently frozen: usgovcc (GOP governor seat counts).
- Order-touching endpoints keep: auth, X-Reprice CSRF header, known-market
  whitelist, 0.1–99.9c price bounds, post-only placement.
  ONE carved exception (owner, 2026-08-22 "Carve it"): the taker dump —
  a limit SELL of held stock priced AT the current bid (never worse),
  only when the spread is ≤2 ticks, only up to the bid's displayed size,
  never below model fair − 3 ticks, exits cancelled first, capped per
  family per day (politics $50, cfb $10). The bond rail (owner's tap
  only) is the other: it opens a bond at the touch not ours (Enter,
  2026-09-02) and closes one into it (2026-09-04, "sell my mass gov rep
  shares to the orders resting at 98 cents"), each level at its own
  price, never more than it shows, never under cost with fees, our own
  exits pulled first. One carve-out (owner, 2026-09-06 "I sold a few
  below cost to free up money... I need a way of doing that with a
  button"): the Sell-under-cost tap on a bond card sells into the bids
  under cost, the loss shown in the confirm and booked as a bond sale,
  the proceeds cash at once. Owner's tap only; the engine never sells a
  bond under cost. The amplifier (owner, 2026-09-09 "amplify my exit
  orders by placing buy orders for the underdogs in markets where I'm
  not meeting my targets using my held shares alone ... it should help
  me earn at the price level where my exit order is"; then "I want it
  to be that the amplifier joins at the exit price. And the fill odds
  seem high. My exits will be bought first, and I don't make that
  many sales. And the amplifier should rest orders for the underdogs
  in each market that do not exceed $26/day. It may exceed that
  summed across markets. earnings does not have to cover it's own
  anticipated loss."): a buy of the underdog (a short beside the lot)
  AT each bond exit's price, behind it in line, so our size at the
  exit's level claims more of the side's reward. In each market the
  largest size the money allows whose expected loss stays under 50%
  of the bonds' average daily earnings; across markets the caps add
  up; the earnings it adds are shown, never a gate. Its fill odds are
  the exits' own record: how often an exit sold over the last 7 days,
  times the share of those sales big enough to take the exit ahead
  first. What the exchange funds of it is what rests — a resize it
  trims rests at the funded size, one it refuses waits out the
  cooldown (2026-09-10: the resize had demanded the full size and
  placed-and-pulled a 3,000-share order every cycle). An exit fill
  pulls it for two hours; the amplifier itself trading books the
  lot's shares sold and pulls the rest; the bonds switch off pulls
  it. Nothing else may cross.
  KILLED (owner, 2026-09-10 "Kill the amplifier. There is a bug that
  bought 3000 shares of yes on a 56 seat senate market ... At 5
  cents which is a total risk for 150 dollars. That is way way way
  past the 28 dollars I told you to set."): the cap had bounded
  expected loss a day (fill odds x loss a share x size), never the
  collateral, and a 3,000-share bid at 5c filled 20 minutes after it
  rested. AMP_ENABLED is False: nothing rests, every resting
  amplifier is pulled each cycle, the page says so. Do not bring it
  back without a cap on the money at risk and the owner's yes.
- Bond money (owner, 2026-09-06): the bonds buy within the budget
  market by market — in any one market, what is held at cost plus that
  market's buy orders stays under the budget; across markets the orders
  may add up to more. And no one market holds more than 50% of the
  budget (owner, 2026-09-07 "max 20% of the budget is going to any one
  market"; 2026-09-08 "Set the per market cap on bonds to 50% of the
  total budget"): a market's buy orders, his Enter and the sniper's
  take all fit that share less what the market already holds. The exchange's free money only sizes an order,
  never gates one. "The only money to deploy automatically for bonds
  is the budget" (owner, 2026-09-10): buys draw on the budget alone, a
  sale returns the lot's cost to the budget, and sale proceeds are
  his — shown as money returned, never redeployed by the engine.
  Politics and cfb may buy only with free money beyond
  what the bonds may still spend (their room). No engine pulls a
  resting bid to "free money".
- NO SCHEDULED GitHub Actions (owner, 2026-08-24: "remove the GitHub
  automation. It keeps running and I keep getting emails"). Every cron
  workflow is deleted; the monitor writes rewards.csv, fills.csv,
  trades.csv, estimates.csv, the Silver tables and STATUS.md itself.
  The remaining workflows are manual-dispatch or push-path only and
  never fire on their own. Do not add a cron workflow — put the work
  in the monitor's publish loop instead.
- Alerts go through ntfy; the topic name is a password.
- The earnings graph and the day's estimate bill nothing across a
  gap in the sampler longer than five minutes (owner, 2026-09-11
  "Make the earning estimate during the period of maintenance 0" —
  the graph had plateaued at the last reading across the exchange's
  maintenance): the gap counts as stale time, the graph reads zero
  across it, and a series carries a reading forward for three
  minutes at most. The same while the exchange is out of reach: past
  five minutes without a full cycle the orders in hand are
  unverified and the meter bills nothing on them (the maintenance of
  2026-09-11 had cancelled every order while the records stood and
  the books read fresh, and $111 was billed across it — owner: "the
  estimate of earnings of today still includes the period of
  maintenance where it seems unlikely the earnings will actually
  show up"); that span was taken back out of the day once, and the
  page shows such a correction under "earned today".
- The market survey is GONE (owner, 2026-09-10 "Take off the survey
  entirely. Save the data on GitHub but i don't need it"): nothing
  samples, nothing enumerates the exchange's programs, no survey tab,
  no survey state. Its last table is
  data/survey_prefix_stats_2026-09-10.csv (with the raw .json beside
  it) and the older rows are data/survey.csv. v3/survey.py stays only
  for the shared math (walls, collateral, live-event checks). Do not
  bring the survey back without the owner's yes.
- The focus tender (owner, 2026-09-10 "With the dramatic rise in the
  rewards pools of some of the markets, we need to put our focus on
  them"): every politics market whose program pays $250 a day per
  event or more (the midterms tiers and the elections boost) is the
  focus tender's — v3/focus.py, its own thread every 15 s, its own
  switch, the /focus page. The engine treats that ground as frozen
  (places, pulls and reprices nothing) and it charges no family
  ceiling; the bonds hand those markets over (a lot already held keeps
  its place in the ledger, the record books its sale). The list is
  sorted by the expected value of an entry of 10% of his buying power
  at the best price ("expected value with fill odds and considering
  the cost of capital tie up"). The page (owner, 2026-09-11 "Make the
  list of markets more condensed. Let me open the more detailed info
  if I click on them") is one line a market — the name, what every
  order there earns a day, the shares held, the drop from the
  eight-hour peak of the rate — and a tap opens the book with his
  orders marked, each order's earnings and every control, the
  tender's arithmetic folded under "the tender's math"; it sorts by
  entry EV, name, earning, holding, drop, or unqualified sides first
  ("Maintenance may also result in many markets being unqualified
  for a while"), a side reading unqualified while what rests on it
  is under the program's target. The exchange's maintenance of
  2026-09-11 cancelled every resting order, his qualifying walls
  included, and nothing re-places a wall on its own; after it the
  owner said "Be careful of placing orders after the maintenance.
  Don't sell everything for pennies there might not be any orders
  resting" — the floors that hold: an exit never sits under both his
  fair and the cost, an entry's concession past fair is charged in
  full, a side under the target earns nothing in the model, and the
  old engine's exits never go under their cost-based floor. Added
  that hour, after the tender sold 84 shares of Ohio Senate dem at
  46c against his 62c fair thirteen seconds after resting them (a
  bare ask side had made the order read $360 a day): an entry may
  sit past his fair only on a side with company, where what others
  rest within six ticks of the side's best adds up to the full stake
  or more; on a bare side it rests at his fair or better, and one
  resting past fair there comes off at once. Each open market
  carries the bonds page's qualify button for a side under 125% of
  the target (owner, 2026-09-11 "Give me a button similar to the
  one on the bonds page that lets me automatically qualify the ask
  side"; the bid side has its own beside it): the same wall run,
  his hand's orders at the far edge of the book, which the tender
  leaves alone and counts back into the stake's buying power. The
  buying power on the page is read every twenty seconds and shown
  with its age; a failing read keeps the last number and says so,
  and the stake line shows its basis — the highest read of the last
  thirty minutes plus what the walls hold (owner, 2026-09-11 "The
  buying power number is out of date"), and beside it the exchange's
  own arithmetic from its balances row: cash less the margin its
  orders and shorts hold (12:11Z: cash $3,085, held $2,950, free
  $122 — the app's "available" is the same figure). An entry or a
  move of one is not sent when its collateral is more than the
  buying power free (a move needs the replacement's while the
  original still rests): 126 placements were rejected "placed but
  not resting" in the hour the account sat fully deployed; the side
  is logged "no_money" once a cooldown and the page counts the
  orders waiting. Exits are never held back. His taps on the page
  (place, move, cancel, pull) call the exchange OUTSIDE the tender's
  lock and take it only to record the result, and the page's
  re-freeze after a tap gives up after three seconds rather than
  wait out a pass (owner, 2026-09-11 "No answer from the server in
  time" on a placement — the pass was placing and verifying orders
  of its own for a minute and his tap waited behind it). A refused
  placement waits out the cooldown before another try on the
  new-entry path too (12:29-12:34Z: four orders refused every pass,
  13 in four minutes, each a placement, a twelve-second verify and a
  withdrawal — the cooldown had been set but never asked there),
  an exit the shorter exit cooldown; the refusal's note carries the
  exchange's own answer to the placement and whether the withdrawal
  found the order.
  Every order in a focus market — his,
  the engine's, the tender's — is his to place, cancel, move and
  resize from the page. The tender rests one order a side ONLY where
  he has set a fair for that market and keeps the expected loss
  (collateral x fill odds) under $1,000 across its orders ("For 6
  expected loss of 1000"). An entry may sit past his fair ("A bid over
  fair value is fine as long as it is appropriately sized for the
  risk and rewards it can earn"): the concession past fair is charged
  in full as a fill cost and the size is chosen with the price. After
  an entry fills, the rest of it comes off and that side rests nothing
  new for fifteen minutes, then re-enters at a quarter of the stake
  and ramps back to full size two hours after the fill ("The stand off
  after a fill for a side should be 15 minutes and when entering size
  should be scaled down until the two hour window has passed"), and
  an entry never adds to a position past the stake (2026-09-10: an
  ask at the touch of the House dem control market filled eleven
  times in fifteen minutes while the tender re-rested it). An exit
  joins the touch and never sits under BOTH his fair and the
  position's cost: whichever of the two lets it nearer the touch is
  its floor, so his fair alone brings it to the touch and the cost
  alone never keeps it away ("if an exit is not earning, then it
  should be placed closer to the touch" — an exit held at its 59c
  cost with his fair at 49c and the market at 53c earned nothing);
  exits are never held, never scaled and follow the lot within a
  minute ("Exit orders should never be held and don't need to ramp
  up. They can always be placed"); his cancelling one by hand does
  not stop the tender ("just because I cancel does not mean that the
  tender should stop"). His qualifying walls (1c bids, 99c asks)
  never impair the tender ("My qualifying orders (1c or 99c) should
  not impair the tender from placing orders"): they count as offering
  none of the lot, the money they hold is counted back into the
  buying power the stake follows, and their size stays on the book
  in the model: a 1c bid of 25,000 is what carries a side to the
  25,000 target, and with it stripped the whole bid side had read as
  earning nothing (22:35Z: the balance-of-power exit at the touch
  showed $0.00 a day). Only the order a plan replaces is netted out. The expected-loss cap counts
  entries only: an exit is the position leaving, counts nothing and
  is never pulled by the cap (21:00Z: a cover was rested and pulled
  "over the cap" eight times in four minutes); the cap has slack —
  the trim starts only 10% over, and an order rested in the last
  five minutes is pulled only when the cap is 25% over (23:32-23:35Z:
  orders rested under the cap were pulled three minutes later as the
  readings moved, 25 such pulls in an hour); his orders the tender
  adopts count toward the cap like its own and are logged "adopted";
  the cap's room goes by value (owner, 2026-09-11 "Yes to value
  ranked cap allocation"): entries rank by expected value a day per
  dollar of expected loss, and a plan that does not fit displaces the
  weakest resting entries only when it beats each of them by double
  or more, at most three at once, none rested in the last half hour;
  a displaced side waits out the cooldown before re-entry and
  displaces nothing itself for an hour (01:00-01:38Z, 2026-09-11, at
  a quarter's margin and a five-minute grace: 39 displaced and 49
  rested in the hour — a plan is valued on a book without it, a
  resting order on the book as it turns out, so every plan looked
  better than every resting order and they rotated); the tender
  judges a resting order by ITS OWN reading of it, never by the
  live_ev/live_pf the family's rescoring writes on the same record
  every minute (01:38Z, 2026-09-11: a bid the tender rested at +$168 a
  day read −$697 a minute later, a 62c fill cost a share, and the
  weak-order pull and the cap's ranking had been reading that);
  the position feed lags a fill by a read or more, so an EXIT of
  the tender's that vanished counts as filled for the position's
  sake, toward flat and never past it — for two and a half minutes
  unconfirmed, five once the journal books it, or until the feed
  itself moves; an entry that vanished counts only once the journal
  books it (05:38Z, 2026-09-11, Florida governor rep: a 1,115-share
  ask's record went missing for a read, was taken as filled, and the
  cover was sized to 1,252 against a short of 137) — and a fill the
  feed already shows is never counted twice, a market the feed
  carried no row for a pass ago having been flat (03:57Z, 2026-09-11:
  a fresh short of 337 read as 674 and the cover was sized to it)
  (02:57-02:59Z,
  2026-09-11: the House rep control exit of 68 filled, the feed still
  showed the lot, a second exit of 68 rested within the minute and
  filled: flat became short 68, the same shape that flipped positions
  at 15:38Z and 21:05Z the day before); an id the tender itself
  cancelled or replaced is remembered for ten minutes and never
  adopted back when the open list shows it late (00:05Z, 2026-09-11: an Iowa governor ask was "adopted" four
  times in an hour, each a ghost of its own move); a cover of a short is
  placed as the close it is (SELL_SHORT), never as a fresh long. A
  refused placement or resize waits out the cooldown before another
  try (a refused resize had been retried every twenty seconds). An
  order the desk placed but never saw resting is withdrawn at once,
  its id stays the tender's, and a fill of it in the meantime is
  booked to the tender (20:09Z and 20:24Z: two such orders filled as
  "your own trade"); the verify note carries the exchange's own state
  for it. The markets in FOCUS_SILVER_FAIRS (his "little hanging
  fruit markets ... except for Alaska set the fair to Nate Silvers,
  the silver bulletins model") get Silver's number as their fair
  once, the first pass the model has one; a fair he clears stays
  cleared. A market
  without a fair is
  shown with Silver's number as a suggestion and nothing is tended
  ("Yes for 5"). The fill cost never reads under 2c a share ("The fill
  cost for politics has been high recently. Keep that in mind").
  Alaska governor is on the tender's ground ("Add Alaska gov") while
  the engine still avoids it; the balance-of-power books stay his
  hand's; held ground stays held until he opens it. The old engine
  keeps cfb, NFL and the unboosted politics markets on $250 of
  expected loss in total (politics $150, cfb $60, NFL $40) and 40 of
  the stream's 200 subscriptions; the focus markets seat first.

## Evidence and predictions (owner, 2026-08-23)
- "We want verifiable and testable predictions and we want to keep
  getting closer to the goal of stable and high earnings."
- Write predictions down in v3/PREDICTIONS.md BEFORE the data lands:
  the claim, why, and what would falsify it. Grade them against the
  exchange's own files. Wrong ones stay on the page with what they
  taught; nothing is quietly deleted.
- "Be wary of anything that is opaque or not giving you the
  information you need. It's always out there for you to find."
  When a number cannot be checked, go find the source rather than
  inferring — probe the API's real response shape, log the fields,
  read the record. The 2026-08-23 probe found the exchange had been
  handing us placement times, cancel reasons, and commissions all
  along while we read five fields of twenty-four.
- The owner will help get access when it is genuinely blocked. Ask.

## Scope and secrets
- Markets: US politics, plus only categories the owner explicitly asked
  about (some sports futures have been surveyed). NEVER econ markets.
- New markets (owner, 2026-09-09: "Can you give me a report on any
  newly added markets?" ... "Yes state races should be included. But
  don't place orders in these races before I get the chance to look
  at them"): discovery alerts him by ntfy and the status page lists
  the week's new markets. State races (attorneys general, lieutenant
  governors, secretaries of state, state supreme courts) and the
  House popular vote markets (winner and margin buckets; owner,
  2026-09-09 "Those should be included but again, don't place any
  orders until I say the word") are HELD ground for politics:
  scanned, scored and shown, but no order rests there until he taps
  Open for orders on that market. The county winner markets
  (Maricopa, Fresno, Nassau, Broward — every pvwc- market; owner,
  2026-09-10 "The model is buying in new markets that I didn't
  approve") are held the same way: their slugs carry usgub/usse and
  had read as governor and senate ground. On held ground the engine
  pulls its own bids, asks and probes and keeps only the exits of
  what is already held; the bond list carries no held market until
  he opens it. Mayors and territories are not on the ground at all.
- Secrets (POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY, DASH_PASSWORD,
  GITHUB_TOKEN, NTFY_TOPIC) exist only as encrypted Actions/env secrets.
  Never in code, commits, or output files.
- Never put the assistant model identifier in commits, comments, or any
  pushed file.

## Trading style
- The owner earns liquidity rewards by resting orders, not by trading.
  Preference: rest near the touch in LOW-volatility markets where fill
  risk is small. Fills are usually losses here, not wins.
- Positions and orders are real money. Before anything that places,
  moves, or cancels orders: say exactly what will change and get a yes,
  unless the owner already approved that specific action.
- When repricing: place the replacement, verify it rested by ORDER ID and
  minimum quantity, only then cancel the original. Never use /modify — it
  destroys orders (details in HANDOFF.md).
