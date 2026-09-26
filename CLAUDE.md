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
  as he left them. THE SEAT MARKETS COME BACK OUT OF IT (owner,
  2026-09-13 "You can stop cancelling my hand placed orders on the
  seat markets"): on the seat-count books — Republican Senate Seats
  (scc-senate-gop) and Republican House Seats (scc-hrep-rep) — his
  hand's orders are never ADOPTED, and since the tender only ever
  touches what it owns, that one refusal keeps every pull, move,
  resize and trim off them; they stay exactly as he left them and
  still count as company and as cover on the book. One adopted
  before the rule is handed back and its id comes off the tender's
  list (three were, at 21:46:06Z — the log had ten adoptions on that
  ground in the twelve minutes before he asked). Then, an hour later
  ("Keep the tender out of the seat markets"), the ground went
  FROZEN outright; on 2026-09-15 he UNFROZE it again ("Unfreeze
  house and senate seat markets") — see the frozen bullet below.
  THIS RULE SURVIVED BOTH: through the freeze and the unfreeze his
  hand's orders on these two books have never been adopted, and are
  not now. The unfreeze hands the tender its OWN orders there, not
  his.
- FROZEN ground — the engine does NOTHING there (owner, 2026-08-24
  "Don't sell my gop governor count race orders. In fact don't touch
  those"): places nothing, rests no exits, reprices nothing, cancels
  nothing. Whatever is resting stays exactly as it is. This is
  stricter than the avoid list, which PULLS the engine's orders out.
  Currently frozen: usgovcc (GOP governor seat counts) ALONE.
  FROZEN IS THE WHOLE ANSWER, and it binds BOTH desks: the tender
  reads the ground "frozen — hands off", so it rests nothing new
  there and its own orders already resting come off; the engine
  places nothing, rests no exits and cancels nothing. His hand's
  orders are touched by neither. The engine does NOT step into the
  ground the tender leaves — freeze_tokens holds it whether or not
  the tender still claims the market (a test pins that). The
  consequence to know: NOTHING RESTS AN EXIT on frozen ground, so a
  position he holds there is his to work.
  THE SEAT BOOKS ARE UNFROZEN (owner, 2026-09-15: "Unfreeze house
  and senate seat markets"), reversing the freeze he set on
  2026-09-13: scc-senate-gop (Republican Senate Seats) and
  scc-hrep-rep (Republican House Seats) come off the frozen list and
  the tender works them with its OWN orders, exits included — so
  exits rest again on the positions held there, which under the
  freeze had been his alone to work. Two things did NOT come back
  with them. His hand's orders on these two books are still never
  ADOPTED (the 2026-09-13 rule above was separate from the freeze
  and is untouched), so every order of his there, his qualifying
  walls included, stays exactly as he left it. And the OLD ENGINE
  still places nothing there: both books are on the tender's board,
  and the tender writes its whole board into fam.freeze_dyn, so the
  engine is held off by the tender's claim rather than by
  freeze_tokens. One consequence to know: a dust lot on a seat book
  is now the sweep's like any other (it skips freeze_tokens alone),
  and on 2026-09-15 that was one lot, House (R) ≥195 at $0.84.
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
  (places, pulls and reprices nothing — its whole-shares cull
  included: 2026-09-12, 03:39-04:34Z, the exchange trimmed the
  tender's orders to what the money funded, 285.23 shares of an Iowa
  House ask, and the cull retired the fraction twenty times in an
  hour, a fresh placement and a fresh trim after each) and it
  charges no family
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
  rest within six CENTS of the side's best adds up to the full stake
  or more (owner, 2026-09-11 "the tender seems unwilling to consider
  anything beyond fair and seems to be leaving money on the table"
  ... "Good": six ticks had been the measure, and on the 0.1c-tick
  House rep control book that was 0.6c — 627 shares near the 17.6c
  touch read bare against a $139 stake while 13,400 sat 1.5c back,
  and the bid was held at his 16c fair earning nothing where the
  touch was worth about $170 a day; the plan's price search reaches
  the same six cents, every tick near the touch and the resting
  levels beyond); on a bare side it rests at his fair or better, and
  one resting past fair there comes off at once — the resting order's
  test sized by the same room the plan uses (the stake less the
  collateral a position it adds to holds, scaled by the refill
  ramp), never the whole stake (15:08-15:20Z: a Texas governor dem
  bid rested at 21c against a 15c fair and was pulled "no company"
  fifteen times in twelve minutes because the two tests sized
  themselves differently). Each open market
  carries the bonds page's qualify button for a side under 125% of
  the target (owner, 2026-09-11 "Give me a button similar to the
  one on the bonds page that lets me automatically qualify the ask
  side"; the bid side has its own beside it): the same wall run,
  his hand's orders at the far edge of the book, which the tender
  leaves alone and counts back into the stake's buying power. The
  buying power on the page is read every twenty seconds and shown
  with its age; a failing read keeps the last number and says so.
  THE BUYING POWER IS READ, NEVER DERIVED (owner, 2026-09-14, with
  the focus page in front of him: "The number you should use for
  buying power is in the balance rows... You don't have to derive
  it. You can just read it. Right now the derived number is almost
  double the real number" — the page said $839.24 with 41 orders
  waiting for money while the balances row said buyingPower
  $490.92). The stake's basis is the balances row's buyingPower
  less the $300 kept free, full stop. The two derivations laid over
  it are GONE: the highest read of the last thirty minutes (owner,
  2026-09-11 "The buying power number is out of date") and adding
  back what his qualifying walls hold (owner, 2026-09-11 "My
  qualifying orders should not impair the tender"). The walls'
  collateral is money the exchange has genuinely taken, so counting
  it back had the tender sizing entries against money it did not
  have while the money gate — which always read the live number —
  refused them. The page still SHOWS what the walls hold, marked as
  already counted out by the exchange, and never adds it. One thing
  the half-hour high had been doing is now done by a different rule:
  a dip plans nothing new and pulls nothing, so a resting order is
  not cycled out when the balance moves (13:43-13:46Z). And a side
  idle for a zero stake says "no money to spend", never "nothing
  earns on this side" — with the basis read from the exchange, an
  empty side must not read as a bad book. An entry or a
  move of one is not sent when its collateral is more than the
  buying power free (a move needs the replacement's while the
  original still rests): 126 placements were rejected "placed but
  not resting" in the hour the account sat fully deployed; the side
  is logged "no_money" once a cooldown and the page counts the
  orders waiting. Exits are never held back. $300 of the exchange's
  buying power STAYS FREE (owner, 2026-09-12 "Yes to those", his
  diagnosis of the batch drops — "it's probably because the buying
  power got too low for the size of the order": six batches of ~20
  orders left the open list that day, each within a minute of a fill
  with the account at its margin limit): the reserve comes off the
  stake's basis as well as gating each entry, so the tender sizes
  what it may actually spend; under it nothing new is planned and
  what rests stays. An exit takes no buying power and is never held.
  A side with a fair where NOTHING RESTS SAYS WHY — the reward it
  would claim, the fill cost and the fill odds against it, the money
  it waits for, the order cap, or a book too old — recorded per side,
  shown on the page under "sides with a fair resting nothing" and
  logged once an hour (2026-09-12, 20:12Z: 26 such sides had rested
  nothing for hours in silence, and a $73-a-day reward claim on the
  North Carolina senate dem ask read as money left on the table when
  the tender had already priced the fill cost at 8.9c a share and
  scored the slot −$26 a day; of 27 such sides only 2 had positive
  expected value, worth $24 a day between them). NOTHING CAPS THE
  NUMBER OF THE TENDER'S ORDERS (owner, 2026-09-12 "There should not
  be a 40 order cap. Where did that come from"): a FOCUS_MAX_ORDERS
  of 40 had been in the tender since its first commit on 2026-09-10
  and he never asked for it — it duplicated, at a number he could not
  see or set, the two bounds that are his (the expected-loss cap and
  the money the exchange leaves free), and on 2026-09-12 it sat full
  while 26 sides with a fair rested nothing. It is gone, and with it
  the slot-rationing it needed. THE TENDER'S BOOK READS KEEP ITS CLOCK
  (2026-09-12, 21:19-21:42Z: the boot on 174.138.33.47 spent 15.5
  minutes over its first pass — 135 book reads through the gateway's
  retry ladder, 30 s a try and four tries — while the page said "the
  first pass has not run yet"; every book was then stamped with the
  PASS'S start, the desk read them as minutes old and refused "no
  book fresher than 120s", the next pass re-read a dozen and the rest
  went stale, 46 of 85 sides idle "no book"; on every other boot that
  day the first pass took 16-34 s): a read is one try of eight
  seconds, a pass reads for ten seconds at most (45 while any focus
  book is unread), each book is stamped at its own read, a 429 stops
  the pass's reads and holds the next twenty seconds, and the pass
  line on the page says what was read, what failed and what waits,
  with the last failure's own words; "books_slow" is logged once in
  ten minutes. ONE THROTTLE FOR THE WHOLE CLIENT, AND THE STREAM AT
  BOOT (owner, 2026-09-12 "Yes to both", after the new pass line read
  "0 books re-read — held off after a 429 — the exchange is
  throttling this address" with 135 waiting: the gateway answered
  the tender's book reads with HTTP 429 while the families' boot
  discovery — hundreds of gateway reads on another thread — kept the
  limit tripped for the twenty minutes the board took to read): a
  429 from the gateway holds EVERY gateway read on every thread for
  the wait the exchange names (Retry-After, 20 s when it names none,
  120 s at most) — a retried read waits it out, a one-try read (the
  tender's) is refused at once with the wait in its words and the
  tender does not even try while it stands; gateway reads are paced
  across threads (GATEWAY_PACE_PER_S, 2 a second — an api-dev caller
  found bigger gaps beat a fast drip when the endpoint tightened) and the tender's
  take the next slot ahead of the family's; every 429 is kept in
  client.throttles with its Retry-After and noted once a hold, so the
  pace is set from the record. And the stream starts at boot with
  the tender, the focus markets seated first, so every focus book
  arrives over the websocket on api.polymarket.us within seconds —
  the gateway's throttle never touches it. That reverses the
  2026-08-31 rule (the stream waited for the first cycle so the feed
  would not compete with the boot for the GIL and the health check);
  the owner took that cost knowingly. An exit always rests as the
  position leaving — a cover as SELL_SHORT, a sale of the lot as
  SELL_LONG — and a resting entry on the exit's side is re-laid as
  the exit whatever its own reading, so the lot is never left
  unoffered behind a short-opening ask. His taps on the page
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
  exchange's own answer to the placement (its shape is {"id",
  "executions"}: fills at once, which a post-only order never has),
  whether the withdrawal found the order, and the shape of the open
  list read (rows, pages, eof, fields); the open list is read to its
  end, following a cursor or page token when the exchange pages it
  (14:12Z: four markets' orders placed with no execution, never seen
  in the list within twelve seconds, found by their withdrawal); and
  the list is neither paged nor cut: the once-a-boot read-only probe
  (17:24Z) had every variant — plain, limit, pageSize, page_size,
  offset, marketSlug — answer the same 260 rows with no field and no
  header, and the count had moved from 249 an hour before, so the
  list is complete; the four markets' orders "never seen" that day
  were the exchange's own lag after its maintenance, and they rested
  again on their own from 15:16Z. The capped-list machinery (an
  absence on a capped read rules nothing gone, an accepted entry the
  list cannot show is kept "resting unverified") stays in the code
  with its hint switched off — a complete list read as capped would
  keep every absent record on the books and never book their fills.
  The 2028 books were boosted at 16:48Z ("presidential_election_20260911",
  $1,000 a day per event, target 20,000, discount 0.2 — a tick back
  keeps 20%): 60 markets on the tender's ground. Owner, 2026-09-11
  ("2028 markets got boosted" ... "That sounds good"): the party pair
  and the candidates priced 5c or more get their midpoint as their
  fair once, the first pass the book shows a bid and an ask within 6c
  with a mid of 5c or more (FOCUS_MID_FAIR_TOKENS), and the tender
  works from it; the penny candidates (1c bid, 2c ask) get no fair —
  their play is his 1c bid wall from the qualify button, which is the
  touch and the whole bid-side window at once; a fair he clears stays
  cleared; and the stake on a 2028 book is $20 of collateral ("Because
  there is no model, keep maximum loss per market on 2028 markets to
  $20"), which bounds each entry and the position an entry may add
  to — a stake he sets by hand on a market stands as he set it.
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
  is placed as an entry is (owner, 2026-09-11 "This is another one
  where it seems it's not going below fair" ... "Yes" — Iowa Senate
  rep: 201 held at 68.6c, his fair 63c, the ask touch 61c, and the
  exit sat at his fair two ticks back earning $19 a day where the
  touch was worth about $140): its price is the slot with the best
  EV from the touch back, it may sit under his fair (a sale under it,
  a cover over it) where the earnings beat the concession charged in
  full and only on a side with company within six cents of the
  side's best adding up to the exit's size, and on a bare side it
  rests at his fair or better (one under fair that lost its company
  moves back after the exit cooldown); it never sits inside the
  touch; the position's cost never holds it and never permits a
  concession either — until that day the floor had been whichever of
  his fair and the cost let the exit nearer the touch ("if an exit is
  not earning, then it should be placed closer to the touch" — an
  exit held at its 59c cost with his fair at 49c and the market at
  53c earned nothing), and the cost is still shown beside the exit;
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
  a fresh short of 337 read as 674 and the cover was sized to it;
  21:33Z: a bid filled 192 as the build booted, the first pass had no
  feed "a pass ago" and added the journal's 192 to a feed that showed
  it — so on the first pass after a boot the feed is the truth and a
  booked fill is never added)
  (02:57-02:59Z,
  2026-09-11: the House rep control exit of 68 filled, the feed still
  showed the lot, a second exit of 68 rested within the minute and
  filled: flat became short 68, the same shape that flipped positions
  at 15:38Z and 21:05Z the day before); a market the position feed
  drops whose newest journal fill closed the position is flat at once,
  never "kept at its last value" (18:34Z, 2026-09-11: 158 Senate
  control and 50 House control exits rested eight minutes after the
  whole position had sold — the feed-short guard had kept the sold-out
  rows for five minutes; the one-order-a-side and orphan-exit guards
  caught both, neither filled); the tender's id list
  keeps every id still resting, however old (20:37Z, 2026-09-11: an
  Ohio Senate dem cover rested at 11:12 had been trimmed by age,
  read as his, and when the open list dropped it for a read a second
  cover of 84 rested against the short of 84), and an exit side his
  orders already cover pulls the tender's own exit there — shares
  are never offered twice; an id the tender itself
  cancelled or replaced is remembered for ten minutes and never
  adopted back when the open list shows it late (00:05Z, 2026-09-11: an Iowa governor ask was "adopted" four
  times in an hour, each a ghost of its own move); and an order the
  tender moved or pulled off a side is netted OUT of the book for
  three minutes like its resting orders, where the level still shows
  at least its size (owner, 2026-09-12 "Yes, do the
  ghost netting": the exchange's book showed the old order for a
  read or two after a move, the tender no longer owned it, so it
  read as company and as the touch, and the New York governor rep
  cover flipped 4c<->10c twenty times an hour chasing its own
  shadow; a book read three minutes after the cancel is taken as
  clean; at a minute — 16:41-16:44Z — the memory expired at the very
  pass the exit cooldown let the cover move again, on a book read up
  to 45 s earlier, and it flipped 8c<->10c every minute with the
  netting in place; a level showing less than the ghost's size has
  already lost the order and is left as the others');
  while the exchange refuses this address's placements as a VPN
  nothing comes off that could not come back — the weak-reading pull
  is paused, and only a cancel that reduces risk still runs (a
  duplicate, a hold, an order past his fair on a bare side, the
  close-out's own, his taps), which is what the page and the alert
  already promised (2026-09-12, 17:09-18:09Z: the boot took
  157.230.234.13, an address the exchange had called a VPN on 09-10,
  60 placements were refused and none rested, and the weak pull took
  the tender from 39 orders to 18 and the day's rate from $1,110 to
  $403 while he had not yet tapped Deploy for a new address). THE OLD
  ENGINE holds the same line (owner, 2026-09-12 "Yes to those"):
  while placements are refused it cycles nothing out for earning
  little, and only the size-past-fair pull, which reduces risk, still
  runs (19:14-19:39Z: it cycled 20 orders out mid-block, 18 of them
  in one second, with no placement possible);
  a cover of a short is
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
  THE BEST ENTRIES MAY GROW PAST THE 10% (owner, 2026-09-15: "Make it
  so the top 25% of ev entries can go above the 10% entry cap to 25% so
  long as the marginal share along the way would be in the top 25%").
  The 10% stake was a flat cap that priced a slot worth $12 a day on
  $100 of collateral like one worth 20c, and on 2026-09-15 it was the
  binding constraint on size: the near-touch collateral was $4,791
  against a stake basis of $360, and the Senate-50 bid that would have
  claimed 29% of its side for $36 was refused "holding $67 here already
  — no entry that adds to it". So an entry in the TOP QUARTILE by the
  cap's own currency — expected value a day per dollar of expected loss,
  the same measure the 2026-09-11 value-ranked cap allocation uses — may
  grow from FOCUS_STAKE_FRAC (10%) toward FOCUS_STAKE_FRAC_TOP (25%) of
  the same basis. It grows ONE SLICE AT A TIME and each slice must clear
  the top-quartile line ON ITS OWN, valued at the margin (the change in
  expected value over the change in expected loss). The reward claim
  saturates with size — our share is our size over the side's, so the
  second dollar buys less than the first — while the fill's cost and the
  capital's do not, so the marginal value falls as the order grows and
  the walk stops by itself, usually well before the 25%; a slice that
  does not raise the expected value at all stops it too. The cut is the
  75th percentile of the entry values of the last half hour
  (FOCUS_VALUE_WINDOW_S), and under FOCUS_VALUE_MIN_N readings there is
  no cut and NOTHING grows. Price is never touched: this decides SIZE at
  the slot the plan already chose. Growth takes the same position bound
  and refill ramp as the base stake (room_top runs through _entry_room),
  so it cannot walk around the standoff after a fill or the rule that an
  entry may not add to a position past its stake. Three bounds are
  untouched and still bind first: the $20 on a 2028 book, a stake he set
  by hand (for those the ceiling IS the base and nothing grows), and the
  money gate — an entry whose collateral is more than the buying power
  free is not sent, so growth can never overcommit him. The card says
  what grew, from what size to what, in how many slices, and the line
  it cleared.
  A POSITION'S COST CARRIES THE SIGN OF ITS QUANTITY, so a basis is
  always a price a share (owner, 2026-09-12 "Yes fix that", after the
  audit he asked for): the exchange reports the cost as the money tied
  up, POSITIVE for a short as well as a long (Massachusetts governor
  rep read as -209 shares at +$196.72, New York governor rep as -386
  at +$311.46), and stored as given a short's basis came out NEGATIVE,
  so covering it recorded a per-share loss of the price PLUS that
  basis — about a dollar a share on a contract that settles between 0
  and 1. The fill model had learned 54.13c a share on the governor
  books and 238.05c on the house seats, against a markdown measured
  from real fills of 1.03c and 1.88c (1,406 and 68 marks), and since
  the model charges whichever measure is larger it was rejecting 59 of
  the 71 sides it refused as "not worth resting"; of 27 untended sides
  only 2 had positive expected value where 18 do at the measured cost,
  worth $217 a day on $3,452 of collateral. Three rails now hold: the
  cost takes the sign of the quantity wherever the feed writes it and
  22 stored rows were repaired on restore; a round trip is learned
  only from a basis that is a price and only when the cost per share
  falls between 0 and $1, the rest counted in trip_dropped; and no
  single close may own a pool's number — the EWMA weight stops at
  TRIP_W_MAX (0.25) where min(0.05 x qty, 1.0) had let any close of 20
  shares or more replace it outright, so the "average" was the last
  big close. Every stored trip cost was learned through that bug and
  is dropped once (TRIP_REPAIR) to be relearned clean.
  Owner, 2026-09-12 ("The fair amount shouldn't affect the fill odds.
  The fill odds should be based on the shape of the book. The
  concession should affect the ev but make the concession as if I
  sell it back midway between my fair price and the current price"):
  the fill odds read the book alone — no bait for a slot past his
  fair — and the concession charged, entry or exit, is what a fill
  loses when the position is unwound midway between his fair and the
  side's current price (the New York governor rep cover: short 392,
  fair 8c, the bid 10c x4.7k — at 10c the charge is 1c a share, not
  2c; at 9c nothing; until then a 2c concession had pushed the fill
  odds toward certain and a 4c lottery ticket earning nothing beat
  the 10c touch earning $9.62 a day, and the cover sat at 4c for
  three hours).
  Alaska governor is on the tender's ground ("Add Alaska gov") while
  the engine still avoids it; the balance-of-power books stay his
  hand's; held ground stays held until he opens it. The old engine
  keeps cfb, NFL and the unboosted politics markets on $250 of
  expected loss in total (politics $150, cfb $60, NFL $40) and 40 of
  the stream's 200 subscriptions; the focus markets seat first.
  SCALE THE NON-FOCUS GATEWAY READS BACK (owner, 2026-09-13 "Scale
  everything that could be causing the 429s back", the meter frozen
  at $359.63 while the old cycle ran 35 minutes): the 429s are the
  old-engine families reading candidate books through the throttled
  gateway — 163 a cycle across the five families, ~115 of them the
  four sports families that hold nothing and earn $0. A family now
  spends NO gateway read discovering candidates it cannot act on: its
  _refresh_books candidate scan runs only when it will place entries
  this cycle (its switch on, the master on, not a flatten); an off,
  master-off or flattening family reads only what it HOLDS (staleness-
  gated, for the exits) and re-plans from cache where replan_s is set,
  so the page keeps its scores without a fetch. The per-cycle book
  budgets are cut too: politics 48->24, cfb 48->16, nfl 40->16, nba
  20->10, gameday 24->12 (scan_reserve halved with them). The tender
  is untouched — it rides the websocket, and the boosted-market
  discovery it needs is refresh_universe/refresh_terms, not the book
  scan. This unfreezes the meter: the shorter cycle keeps the
  sampler's lock free and the estimator's verified clock inside its
  five-minute window (2026-09-13: the 21-35 minute cycle had locked
  the sampler out and zeroed every reading past five minutes).
  THE BONDS TAKE THE SAME RULE (the same instruction, "everything";
  I had applied it to the five families and missed this one, found on
  the 17:52Z check): bonds._refresh_books read up to 40 books a cycle
  across its whole listed set, ungated by its switch, at a 60-second
  staleness threshold that a 6-15 minute cycle puts every market past.
  Its switch had been OFF for 32 hours and the pass was still reading
  61 listed markets — 431.8 s of a 676 s lap, the biggest single item
  — while the tender read 0-2 books a pass with 57-80 due and the
  gateway answered with SIX 429s a minute. With the switch off it now
  reads only what it HOLDS or has an order resting in (10 markets, not
  61); a row it stops reading keeps its last book on the page and is
  flagged stale past ten minutes, which the page already promised.
  THE METER READS LIVE REGARDLESS OF CYCLE LENGTH (owner, 2026-09-13
  "Yes build both", after the scale-back left the cycle variable at
  2-9 minutes and the meter billing only ~8% of the time): two fixes,
  meter-only, no trading touched. (1) The 20-second sampler no longer
  takes the cycle's lock — the cycle held it for its whole run, so the
  sampler blocked and sampled once a cycle instead of every 20 s; it
  now reads a defensive snapshot of the orders lock-free (a read caught
  mid-mutation retries once, then is skipped, the next tick 20 s away)
  against the live book cache the stream feeds. (2) The "orders still
  resting" clock (verified_at) is stamped at the open-list read, early
  in the cycle, and given its own 10-minute window (VERIFIED_MAX_S)
  separate from the 5-minute sampler-gap rule: the open list is read
  once a cycle, so a healthy 6-9 minute cycle no longer reads as an
  outage. A real outage past 10 minutes still bills nothing, the
  owner's 2026-09-11 rule.
  AND THE CLOCK NO LONGER WAITS FOR THE CYCLE (my own defect, found on
  the 16:29Z check: the cycle is not a clock — with the master back on
  the laps ran 6 to 15.6 minutes, 711.9 s on the 16:12Z lap alone, so
  the stamp went past even the widened ten minutes and the meter still
  blanked one 20-second tick in six on a healthy run). The sampler
  reads the open list ITSELF when the stamp is older than two minutes
  (VERIFY_PROBE_S) — one try, ten seconds, on the SIGNED trade api,
  never the throttled gateway, so it costs the books and the tender
  nothing — and the window goes back to the owner's own five minutes.
  A probe that fails, or that answers "nothing resting" while we hold
  records (the 2026-09-11 maintenance shape), does not stamp, so a
  real outage still bills nothing.
  THE DUST SWEEP (owner, 2026-09-13 "Let's just do the holdings that
  are less than 1 dollar when valued at a midpoint price between the
  bid and the ask. If the difference between the bid and the ask is 1
  cent, then sell at the ask. Otherwise list at the midpoint. If the
  midpoint is not a whole number you can round toward the price that
  it could be sold at" ... "Replace all my hand orders" ... "Place once
  and leave it. Give me a button to run it again"; his goal "pull cash
  out so I can reevaluate and try to simplify my operation" — the
  exchange held 187 positions with shares across politics, cfb and
  nfl, most of the sports ones a handful of shares): v3/sweep.py, a
  card on the switch page. Every held position in EVERY family whose
  shares x live midpoint is under $1 gets ONE exit at his rule — a
  long sells at the ask on a one-tick spread, else at the midpoint
  rounded DOWN to the tick (toward the bid, the side it could be sold
  at); a short buys back at the bid on a one-tick spread, else at the
  midpoint rounded UP (toward the ask) — sized to the whole lot,
  post-only, never inside the touch. Every order already on that side
  comes off first: the engine's, the tender's AND HIS HAND'S (his
  carve-out from the untouchable rule, for the sweep alone), except
  his qualifying walls, which by the house convention offer none of
  the lot. Placed once and left; the button runs another pass; nothing
  runs on its own — Preview reads the books and shows every order it
  would place, Place (with a confirm) places them, both audit-logged,
  a run saved and pushed to the phone at once. Frozen ground and the
  close-out ground are skipped. The order rests as purpose "sweep":
  the engine treats it as hands-off exactly like his own (never
  cancelled, moved, trimmed or nursed; charges no ceiling; netted out
  of every exit it sizes), _reclassify_exits never relabels it, and the
  tender counts it as cover on the exit side and never adopts it — so
  the lot is never offered twice and never moved off his rule's price.
  A refused placement is reported on the card; its lot is left for the
  engine or tender to re-offer on their next pass.
  THE ENGINE'S HANDS-OFF WAS TRUE IN SOME PATHS AND FALSE IN FIVE (my own
  defect, found on the 19:00Z check of 2026-09-13 after the first real
  run): the SHORT-cover sum did not name "sweep", so a sweep cover of a
  short read as nothing, `rest` came out as the WHOLE short and the
  engine rested a second cover beside it — the lot offered twice in TEN
  markets (Connecticut, Hawaii, Illinois, Massachusetts and Wyoming
  governor rep, Illinois senate rep, four cfb win totals), which is the
  shape that flips a short long when both fill. The long side had been
  right all along. Four more paths took a sweep order for their own: the
  kickoff pull cancelled it (kickoff_pull defaults True, and 89 of the
  104 orders were cfb and nfl), the whole-shares cull retired a
  fractional one, the ceiling's trim could cancel it to get under a
  ceiling it never fed (it is an _owner_exit, so it charges nothing),
  and the aged-order path advanced the blank-market probe ratchet off
  it. All five now name "sweep". The prune that cleans up an
  over-covered side only ever cancels purpose "sell", so the ten live
  doubles heal themselves on the first cycle of the new build.
  EACH ORDER IS PRICED ON A BOOK READ THE SECOND IT GOES ON (owner,
  2026-09-13 "It skipped almost everything because the books were old.
  Can you read the book immediately before placing" — the 16:51Z run
  placed 7 and was refused 95, every refusal "no book fresher than 120s
  — refusing to place blind"): the plan had priced all 102 holdings up
  front and the placing loop that followed took minutes, a cancel and a
  placement a market through a gateway answering 3-4 429s a minute, so
  by the fiftieth order the book it was pricing from was minutes old and
  the desk's own freshness gate threw it out. Now the plan only picks
  the candidates; each order then gets its own read (one try of eight
  seconds, three tries so a 429 is waited out rather than losing the
  lot, the tender's priority) and its price, its midpoint and HIS DOLLAR
  are all taken from that read — a lot the fresh book puts at a dollar
  or more is left alone and said, a one-sided book likewise, a failed
  read reported with the exchange's own words. A book the stream
  delivered within thirty seconds already IS that read and stands, so
  the focus markets spend no gateway read. What rests on the exit side
  is re-read from the live orders at the same moment, not from the plan,
  so an order laid there since comes off too and the lot is never
  offered twice; the sweep's own order from an earlier run is replaced
  like any other, which is what running the button again means. And the
  plan stamps every book at its OWN read, never at the pass's start —
  the tender's 2026-09-12 lesson, which the sweep had repeated.
  THE TAP ANSWERS AT ONCE (owner, 2026-09-13 "Nothing is happening
  when I click preview the sweep"): the first tap read ~120 books
  through the throttled gateway ON THE WEB THREAD and the page, frozen
  to bytes at cycle end, could not show the preview until the next
  cycle, minutes later — so the tap looked dead. Now Preview and Place
  run on the sweep's own thread and the tap answers at once with a
  note; the card polls its own live endpoint, /sweep.json (the same
  shape as /focus.json), every four seconds and shows the progress
  (reading N of M holdings, placing N of M), the preview, the last
  run and any error — never the frozen page payload. One at a time: a
  tap while one runs answers with its progress instead of starting
  another. A run finishing saves the state and pushes an ntfy.
  THE SHARES CROSS (owner, 2026-09-14, correcting my reading of his
  rule: "The original intent of my request for the sweep process was
  to have these shares cross the midpoint to sell because resting will
  do nothing"). I had read "sell at the ask ... otherwise list at the
  midpoint" as a RESTING price and laid every order on the passive
  side of the touch, where it earned nothing and sold nothing: of the
  96 orders the 00:08Z run placed on 2026-09-14, 27 were still sitting
  there two and a half hours later and the exchange's position count
  had fallen by five; 26 of the 27 sat on a ONE-CENT spread and
  crossing all of them would have cost $1.09 in total. So the
  midpoint decides WHICH lots go and nothing else: every holding under
  SWEEP_MAX_VALUE_USD at the midpoint is sold ACROSS the spread — a
  long sells AT THE BID, a short buys back AT THE ASK, the whole lot,
  taking whatever rests there. That is the THIRD carved exception to
  post-only placement (the 2026-08-22 taker dump and the bond rail are
  the others) and its rails are: HIS TAP ONLY, never a price worse
  than the touch (a sale never under the bid, a cover never over the
  ask), and only a close of something already held — never an opening
  order. The size is NOT capped to what the touch shows, because he
  wants the lot gone: what the touch cannot absorb rests at that same
  price, which is the most aggressive resting price the book has and
  strictly better than the midpoint it used to sit at. A crossing
  order FILLS AS IT IS PLACED, so the sweep books what the exchange's
  answer says executed — the position moves, its cost moves with it,
  the fill is journalled — and records only the remainder as resting;
  an answer carrying no executions list at all books nothing and
  leaves the whole lot resting (the 2026-09-12 close-out lesson), and
  the open list and the position feed correct it either way within a
  cycle. The card shows what crossing GIVES UP against the midpoint
  before he taps, and the confirm says the orders trade at once rather
  than resting.
  WHERE THE 429s CAME FROM, AND THE THREE FIXES (owner, 2026-09-14
  "Do all three for the 429s" with the caveat "Unless it would affect
  our ability to estimate earnings from focus markets" — a whole day
  of throttling). The cause was not any one caller: 335 markets wanted
  a live book (the tender's 209-market board plus 173 held by the five
  families) against a stream of 200 seats, so 135 markets had NO seat
  and were read through the gateway every pass, forever. It ate
  itself — 35 429s in 10.8 minutes, and since a 429 holds EVERY
  gateway read on every thread, 314 s of blackout in a 645 s window,
  the gateway shut 49% of the time, which is why the tender's own pass
  line kept reading "0 books read, 72 due — held off after a 429". The
  429'd paths proved it: the tender itself took one at 02:25:10Z
  reading scc-hrep-rep-gte225, a FROZEN seat book, and one at
  02:24:40Z was stsc-hormuz-normal-2026-09-30, a market we hold
  nothing in and have no order in. (1) THE STREAM GETS A SECOND
  CONNECTION: the cap is per SUBSCRIPTION, so STREAM_SHARDS
  connections buy 200 seats each — each shard its own thread and its
  own slice of the one priority-ordered list, so the best markets
  still land on the first and a shard that dies costs only its slice.
  FOCUS_WS_CAP/ENGINE_WS_CAP go 160/40 -> 280/120: the tender's whole
  board fits with room for the old engine's held markets behind it.
  (2) DUST WITH NOTHING RESTING SPENDS NO GATEWAY READ: a held market
  with no order of ours on the book earns nothing — rewards are paid
  on RESTING ORDERS — so its book buys the meter nothing. On
  2026-09-14 that was 60 of NFL's 63 holdings, $19.01 of cost basis
  between them, and NFL's measured rate was $0.00 a day across 0
  markets. A market with ANY order resting keeps its book whatever it
  is worth, and a market we have no book for at all is never judged
  dust: no data is no verdict. (3) FROZEN GROUND KEEPS ITS BOOKS —
  the cut I proposed and HIS CAVEAT KILLED. Frozen means neither desk
  may act, so the read looked free; it is not, because his 72 hand
  orders on the seat books measured $24.34 A DAY on 2026-09-14 and
  the meter prices a resting order off the LIVE book. Frozen markets
  keep their reads and their stream seats deliberately. The rule this
  leaves: a read may be cut only where nothing rests, because nothing
  resting is the only proof that nothing is being earned.
  THE TENDER'S GROUND IS NOT HIS FROZEN GROUND (owner, 2026-09-14:
  "There are more markets that I have <1 dollar in that aren't being
  caught up in the sweep"). The sweep asked the family `_frozen()`,
  which is true for the ground he NAMED and also for every FOCUS
  market — a family marks the tender's whole board frozen so the tender
  owns it, an arrangement between two desks of ours, never an
  instruction from him. So the 04:11Z run skipped 41 markets as "frozen
  ground" and only 12 of them were his (the seat books and usgovcc, and
  by basis not one of those twelve is even under a dollar); the other
  29 were the tender's. A dust lot in a focus market is his to clear
  like any other, and the tender's exit there comes off first like any
  other order on the side. The sweep now skips freeze_tokens alone.
  AND "DID NOT SAY" IS NOT "NOTHING TRADED" (my own defect, same
  report): the run said "0 filled, N resting" for all 88 orders while
  the exchange's position count fell 168 -> 112 in the seventeen
  minutes after it and 44 fills were booked by the ordinary reconcile —
  the orders HAD traded. The answer simply carried no executions list,
  and res.filled is None for that where it is 0.0 for an empty list.
  Nothing is booked in either case, which is the 2026-09-12 close-out
  lesson and keeps the books right, but the card now says which
  happened: "the exchange did not report on these — their fills book on
  the next cycle" rather than a false zero.
- SCHEDULED MAINTENANCE (owner, 2026-09-14: "There is maintenance from
  4-8:30 this morning. Can you pull the orders 15 minutes before and
  start putting them back in approximately their correct spot after
  everything is back online? Don't have to be too aggressive. If you
  can get better prices or higher earning in thinner books do that.
  Stay flexible"): v3/maintenance.py, windows in MAINT_WINDOWS as ET
  wall-clock pairs, ticked off the SAMPLER'S clock every 20 s so a slow
  cycle can never make the pull late. Fifteen minutes before a window
  every order on the EXCHANGE'S open list — not ours, because an order
  of his we never adopted is on it and is exactly the kind that never
  comes back on its own — is written down and cancelled. HIS HAND'S
  ORDERS AND HIS QUALIFYING WALLS COME OFF TOO: the standing rule makes
  them untouchable, and this is his own carve-out for the one case that
  helps him, because the maintenance cancels them anyway (2026-09-11 it
  cancelled every resting order, walls included, and nothing re-places
  a wall on its own — the day after, the whole bid side of the
  balance-of-power book read as earning nothing for want of the
  25,000-share 1c wall that carried it to the target). While the window
  runs nothing places: both desks read the master as off through
  maint.holding(), his stored setting is never touched, and his own
  taps still work. After the window, once ONE signed read answers, the
  orders go back six a pass. Each is priced on a book read for it: a
  wall goes back exactly where it was; a BID at its old price or the
  best bid if that is LOWER (never pay up); an ASK at its old price or
  the best ask if that is HIGHER (never sell cheaper) — so it lands at
  its old spot or at the touch, whichever is better for him, and a side
  that thinned out over the window gives both the better price and the
  higher-earning slot at once. Nothing crosses. An order bigger than
  the desk's QTY_MAX (20,000, and his walls are bigger) goes back as
  however many orders it takes at the same price. A refusal is retried
  six times, then left and reported on the card. The whole run is saved
  with the state, so a restart mid-window picks it up where it was.
  AND AN ORDER THAT NEVER LEFT IS NEVER PLACED TWICE (found on the
  live run of 2026-09-14, before the restore could do harm): the pull
  cancelled 272 of 313 and 29 were still showing when the window
  opened — a cancel can be refused, and the open list lags one either
  way, which is what the desk's own "cancel_again" exists for. Restoring
  those would have offered the same shares twice, the shape that flips
  a position when both fill. Each restore pass now re-reads what the
  EXCHANGE says is resting (not our books, which can be wrong in both
  directions) at most once in thirty seconds; a snapshot row whose id is
  still on that list is marked back where it is and never re-placed, and
  a failed read keeps the last one, so the check can only ever stop a
  placement and never cause one. The live run also confirmed the hold:
  60 placements and 260 exits were refused "master switch is off" in the
  quarter hour after the pull, and nothing rested.
  AND AN EMPTY BOOK IS NOT CHASED (the 12:32Z restore, same day): a
  106-share bid went back at 14c where he had it at 42c, and its ask at
  62c where he had it at 43c, because the North Carolina House 01 book
  had come back 14c/62c with almost nothing resting on it. Nothing was
  at risk — of the first 48 restored, NOT ONE moved against him, and the
  collateral on the restored bids fell from $344.90 to $308.12 — but a
  48c spread is not a price, and "approximately their correct spot" is
  what he asked for. Past MAINT_TRUST_SPREAD (10c) the touch is not
  trusted and HIS OWN price stands, clamped as ever so it cannot cross.
  This is his own rule from the 2026-09-11 maintenance ("Be careful of
  placing orders after the maintenance. Don't sell everything for
  pennies there might not be any orders resting") applied to the way
  back in. The median move over those 48 was 1c, so this changes only
  the tail: 11 of 48 had moved more than 10c and 4 more than 25c.
- THE QUALIFY BUTTON READS BOTH LEDGERS (owner, 2026-09-17, with the
  focus page in front of him: "When I try to qualify the sides on these
  markets I get the error ... no Target Size on record here — reward
  terms not read yet. The target sizes for these markets is 10,000").
  The twelve Texas/Maine and Texas/Michigan/Maine Senate combos
  (cpoc-ussec-tx-…) were listed on 09-16 21:40 with no program; the
  politics family read them within the hour, found nothing, and marked
  them "read — no program", which sends a market to the back of its
  rotation (6,465 markets at 300 a half hour, ~11 h). The exchange then
  put them in Elections Boosted High ($1,000/day), and the tender's OWN
  walk read that at 15:43 and seated them — while the button, which
  read the family's ledger alone, said "not read yet" on the very page
  showing $1,000/day. Three things now hold. The button takes the terms
  from whichever ledger has them, the tender's first. The tender hands
  every program its read finds to the family's ledger where the family
  holds nothing (never over a program it has) — one read of the
  exchange updates both books, logged "terms_handed" — and since the
  family's ledger is the one saved with the state, a restart seeds the
  board from it at once instead of waiting a walk. And the words say
  which thing happened: "the last read found no program on this
  market" when it was read, "not read yet" only when it was not. The
  target is never typed in: it is whatever the exchange's program
  record says.
- THE TENDER CLOSES A TARGET SIZE GAP (owner, 2026-09-17, Senate Combo
  Texas and Maine, the page in front of him: "When there is this much
  money on the table, and the book is this thin, I think it makes sense
  to try and get some of it. Why isn't some portion of the tender
  offering a little above fair?"). The ask side held 9,900 of a 10,000
  Target Size, the touch was 77c x1 and his fair 40c. It was never the
  fair: an ask at 77c is 37c above it. It was the target: a side under
  it pays nobody (his own 2026-09-11 rule), the stake's $11.40 bought 49
  shares at 77c, which left the side 50 short, so every ask read "$0.00
  a day" (the log said so twice) — while 100 shares would have carried
  the side over the line and taken 99% of its $125 a day for $23 of
  collateral, and the 25% ceiling ($28.50) allowed it. The growth walk
  could not find that: it only extends a plan that already scores, and
  a $0.00 plan never starts it. Now _entry_plan also tries, at each
  candidate price, THE SIZE THAT CARRIES THE SIDE OVER THE TARGET (the
  stake's full size plus the shortfall estimate_join reports, plus
  FOCUS_GAP_CUSHION) wherever that fits inside the growth ceiling
  (stake_max, which already carries the position bound and the refill
  ramp), and takes it over the base plan only when it clears the same
  top-quartile line growth answers to — under FOCUS_VALUE_MIN_N
  readings there is no line and nothing closes. Price is the plan's
  own choice as before; the fair bound, the bare-side rule, the money
  gate, the $20 on a 2028 book and a hand-set stake all still bind
  first. The card says it: "sized to carry the ask side over its
  10,000 Target Size: the stake's 49 shares left it 50 short, 100
  closes it".
- THE BOOT NEVER WAITS ON THE THROTTLE, AND NEVER SAVES A FRAGMENT
  (owner, 2026-09-17 "The meter is busted. Nothing is showing up. It's
  been like this for a while", the quick look stuck on "starting up —
  Game day: discovering, reading terms, scoring books"). Both deploys
  that afternoon booted into a gateway answering 429s every pass (the
  tender's own line: "read 3-6 of ~65 due, held off after a 429"), and
  the families' first cycle — politics' 24 book reads, gameday's eight
  event pages a tag, each through the four-try ladder waiting out
  holds of up to 120 s, each failure swallowed and the next read
  begun — ran 45 minutes twice, the page hiding the meter behind the
  boot card the whole time. Worse: a focus-page tap during that boot
  saved {focus, fam_politics, saved_at} over the 72-key state on
  GitHub, because before the first cycle there was no last_state to
  build on, and load_best takes the newest file — a restart would have
  come back with every switch off and the bonds, journals, actuals and
  pay records empty (the full 16:32Z copy was rebuilt and pushed by
  hand before the fix deployed). Three things hold now. Every save
  and every page builds on _base_state(): the last full cycle's state,
  else the one restored at boot, never nothing. The first cycle after
  a boot reads the gateway ONE-TRY like the tender (client.boot_one_try,
  set for that cycle alone): a read under a hold is refused at once,
  the family moves on and rests nothing blind, and the ladder resumes
  from the second cycle — the signed trade api (orders, positions,
  balances) keeps its ladder throughout. And the boot card says how
  many minutes it has sat on a step.
  AND EVERY ORDINARY GATEWAY READ IS ONE-TRY WHILE A HOLD STANDS
  (owner, 2026-09-17 "Yes, ship it", from the 22:10Z check on the new
  build): the boot was 37 s, the next cycles 80-167 s, and then every
  cycle from 18:51Z ran 37-43 minutes — five in a row, 2548 of 2585 s
  in the families' lap — because the gateway was answering 429s on
  /book reads about twice a minute (Retry-After 10 or 0) and each
  family read waited the hold out and retried into the next one, four
  tries deep; the tender's own pass line read "held off after a 429 —
  read 0 of 77" three passes running, and the position purge, which
  runs once a cycle, let a phantom stand 39 minutes (House control
  rep, 20:44Z). Now, while a hold stands, a gateway read without
  priority is refused at once with the hold in its words — the
  families and the bonds keep their cached book and place nothing
  blind — and the ladder is back the moment the hold clears; a
  priority read (the sweep's, maintenance's, the tender's
  placement-time read, his taps) keeps its ladder always, and the
  signed trade api is never touched. This is the boot rule made
  standing, and it also stops the families' retries from feeding the
  hold they are waiting on.
- THE FEED DOES NOT OVERWRITE A FRESH FILL, AND A FLIP TAKES THE
  FILL'S PRICE (owner, 2026-09-18 "Yes, ship both", after the morning
  the exchange wiped every resting order). The exchange's position
  feed lags a fill by a read or more: eleven times that day a cover
  filled, the feed still showed the old short, the book snapped back
  to it ("exchange wins") and purged it a minute later, and in one of
  those windows the engine rested a second cover on the phantom short
  and it filled (Louisiana senate dem, 2 @ 11c). Now, for
  FEED_LAG_GRACE_S (180 s) after a fill WE booked in a market, the
  feed's number does not overwrite the book there — logged
  "feed_lag" once a window — and after the grace the exchange wins
  as before; the purge path already had the same grace on a fresh
  position. The stamp (fam.fill_at) is saved with the state. And a
  fill that FLIPS a position through zero opens the other side, so
  the new side's basis is that fill's price: the 2028 Dwayne Johnson
  lot flipped long -> short through 1c sales, the short kept the
  long's 45c basis, and the dead-short step-up, bounded to 5 ticks
  over "what the short sold for", bought 15 back at 50c on a book
  with no bid and a 51c ask (about $7.35 lost on a penny contract).
  The step-up also never runs on close-out ground now: there the
  engine sells into the bid and rests covers at break-even, and
  never bids up. Still open: a short whose basis came from the
  exchange's cost field rather than our own fills carries whatever
  that field means, and the step-up's ceiling for it is still 5
  ticks over that basis, capped at the bid touch and fair + 3 ticks.

- A STOCK EXIT MAY REST ONE TICK UNDER THE MODEL FAIR WITHOUT BEING
  STRANDED (owner, 2026-09-21 "Yes, let them rest under fair", to
  "The number of orders I have open is very low"). The placer's rule
  since 2026-08-22: a sale joins the ask touch unless the touch gives
  away against the model, then it rests one tick under fair. The
  stranded rule of the same day allowed nothing past touch + 2 ticks.
  The two disagreed wherever the model fair sat more than 3 ticks over
  the touch, and on 2026-09-21 (14:45-15:30Z) that was six lots — New
  Mexico, Maine, Alabama and Nebraska governor, Illinois and Nebraska
  senate, 117 shares — each rested at 97-99c on a 90-95c book (78c on
  68/69 for Nebraska senate) and cancelled a cycle later as stranded,
  23 rounds each, 138 cancels in 45 minutes, off the book half the
  time. The bound now allows the placer's own slot: touch, floor or
  fair − 1 tick, whichever is highest, plus 2 ticks. Without a model
  fair the touch bound stands as before; a leftover past the slot
  still comes off. The cover side never had the conflict (its slot is
  bounded by the bid touch and the cap) and is untouched.
  THE REST OF THAT ANSWER, for the record: on 09-14 the exchange's
  open list held 313 orders, 207 his (85 on the seat books); the
  09-18 wipe took them all and his walls' collateral read $2.30 from
  then on (bonds log), nothing re-places a wall on its own; and the
  tender fell from 61-67 entries on 09-18 to 14 — 40 sides at their
  stake after 39 fills, 61 sides whose allowed slot scores $0.00 a day
  (fair well behind the touch on a bare side, stakes of $12-29 against
  sides of 20k-750k shares), 15 where the 8-20c fill cost eats the
  claim, on a stake basis of $117.85 (buying power $417.85 less $300).

- A BASIS THE EXCHANGE'S COST FIELD SET IS ESTIMATED, AND THE FILL
  MODEL RELEARNS FROM OUR OWN FILLS (owner, 2026-09-21 "Yes repair the
  fill model", after "What does the evidence show?"). The learned trip
  cost per pool over the day's saved states moved right after the fake
  loss alerts: House/Senate control 3.9c -> 8.2c -> 14.8c -> 21.7c a
  share (the last within three minutes of the $74.97 House control rep
  alert), governor 2.3c -> 19.9c within the hour of the two NY governor
  rep alerts on 09-20, Senate 2.2c -> 16.6c after a 44c-a-share TX/ME
  dsweep trip and a 53c-a-share NM senate trip — against measured
  markdowns of 0.4-2.3c. The fake trips came from lots whose cost the
  feed's cost field set (House control rep: 235 shares at 11.2c
  reported as $105.21; sold at 10c that "lost" 41c a share, learned at
  the maximum weight four times in a day; $237.58 alerted on that one
  market, real under $5). The 2026-09-12 guard dropped only a basis
  outside $0-$1, and 51c is inside it. Now: (1) TRIP_REPAIR is bumped
  once ("feed-basis-2026-09-21"), every pool falls back to its measured
  markdown with the 2c floor and relearns; (2) a lot whose cost came
  from the exchange's cost field — seeded from nothing on the
  exchange-wins path, a position the feed shows that we never opened,
  or a stored basis outside the price range on restore — carries
  inv["est"]: its closes are booked and shown (the card says "estimated
  … not paged, not learned"), teach the fill model nothing, page no
  "closed at a loss" and no "under water"; a fill that flips the
  position through zero clears it, since that fill's price is the
  basis; the feed correction that SCALES our own basis keeps it clean.
  The page marks the position "cost estimated by the exchange's feed".
  THE HONEST SIZE OF THIS: of 111 idle sides in the tender's log only
  17 had a positive claim and 7 of those turn positive at a 2c cost,
  worth $0.01-0.40 a day each — the repair stops the fake alerts and
  the churn (11-46 pulls an hour at "expected value −$0.01"), not the
  earnings drop, which is his 207 wiped hand orders (markets with a
  claim 251 -> 117 across the 09-18 wipe) and a $20-50 stake against
  sides of 50k-1.5M shares.
- THE FOCUS LIST SAYS WHAT A QUALIFYING WALL WOULD ADD (owner,
  2026-09-21 "Show me in the list of focus markets which qualifying
  orders I can place to boost earnings"). A side under the target pays
  nobody, so every order of ours there reads $0.00 and the tender rests
  nothing new. For each such side the row now carries boost[side]: the
  side scored again with the wall on the book (the qualify button's
  own wall — 125% of the target less what rests, at 1c or 99c) — the
  resting orders first, else the tender's own entry where it would rest
  one (a fair set, EV over zero) or the exit of what is held — with
  the parts spelled out ("the tender's 100 @ 47c → $12.30"; "a new
  entry of 60 @ 47c → …"), the shares and buying power the wall takes,
  and boost_day for the list: a "wall: +$/day" pill on the condensed
  line, a "wall boost" sort, and the estimate beside each qualify
  button. On 2026-09-21 only 3 of 178 sides with a book in the
  family's cache were under target, so the list may be short; the
  board's other markets are scored live. The wall itself earns nothing
  worth counting; nothing places without his tap.

- WHEN A MARKET LEAVES THE TENDER'S BOARD, ITS ENTRIES COME OFF
  (owner, 2026-09-22 "C: Pull entries when a market leaves the board",
  the one of three fixes he took — A, sealing the 37 pre-flag
  feed-seeded lots, and B, the tender's resize churn, were NOT
  approved and are not built). On 2026-09-21 at 21:41Z the exchange
  cut the Senate Combo pools from $300 to $25 a day; the twelve books
  left the board ("left_focus"), and four tender entries holding $140
  of collateral (399 @ 7c, 257 @ 20c, 38 @ 43c, 104 @ 44c) rested on
  for six hours earning $2.70 a day between them — the tender no
  longer planned that ground and the engine left them alone. Now
  refresh_markets calls _release_entries for every departed market:
  the tender's own orders there that are ENTRIES are cancelled
  (logged "pull … left the board"), the ids go to the ghost memory so
  a lagging open list never adopts them back; an EXIT — purpose
  "sell", or a sale of a long / a cover of a short by the family's
  book — stays for the engine; his hand's orders and his walls are
  never touched. A refused cancel is logged "pull_refused" and left.

- THE STOP SIGNAL SAVES THE STATE (owner, 2026-09-22 "Yes, ship it",
  fix D). The launcher forwards the platform's SIGTERM and v3.main had
  no handler: the process died with nothing written, so every deploy
  lost the orders the tender had placed since the last cycle's save
  (its passes run every 15 s between them). The next boot found them
  on the open list, not in the state, and recorded them as his hand's
  — untouchable — and on the seat books, where his orders are never
  adopted, the tender rested its own beside them: 2026-09-21 a 15-share
  ask on House seats ≥215 (the tender added 21); 2026-09-22 five orders
  at once (Senate seats 52: a 223-share bid at 7c became "his", the
  tender rested 195 at 8c beside it; 48 the same with asks of 7 and
  18 at 15c; House ≥225 and ≥230 bids; the NE senate rep exit of 13).
  Now run() installs SIGTERM/SIGINT handlers: shutdown_save builds on
  the last full state (never a fragment — with nothing restored and no
  cycle run it writes nothing, the 2026-09-17 lesson), overlays every
  part that moves between cycle saves (the families' to_dict, the
  tender, the bonds, the sweep, the maintenance run, the desks' cancel
  memory, the switches, the audit), waits for a running cycle up to
  SHUTDOWN_LOCK_S (5 s) then saves regardless, uploads with
  force_remote and waits up to SHUTDOWN_SAVE_S (25 s) for the upload,
  prints what it did, and exits. A boot then restores every order as
  whose it really is.

- THE STOP HALTS EVERY DESK BEFORE IT SAVES, AND THE BOOT SAYS WHICH
  SAVE IT CAME BACK FROM (owner, 2026-09-22 "Yes, ship it", fix E). The
  first deploy after fix D (22:48Z, stopped by 688d658c, which had the
  handler) still brought one order back as his: the tender's Senate
  seats 50 bid had been 607 at 12c in the 22:18Z save, it resized to
  552 before the deploy, and the boot restored a state without the
  552 — the stop save either never landed or the tender traded during
  it (its thread was never stopped). Now: OrderDesk.halted (one flag
  for every desk) is set first — place_resting/reprice refuse through
  _check and cancel refuses, logged "stopping"; cancel_all, the
  emergency stop, stays exempt — then shutdown_save takes the cycle's
  lock and the tender's lock (5 s each), snapshots, and writes
  state["last_stop"] {at, why, build, keys, cycle_lock, tender_lock,
  halt_s}. At boot, _restore sets boot_restore {"from": "the stop save"
  when the restored state carries a last_stop stamped at its own
  saved_at, else "a periodic save — the stop save did not land",
  saved_at, age_s, stop}, and every cycle's state carries it, so each
  check can say which save the boot restored.

- THE TENDER LOGS THE BOOK ON EACH MOVE (owner, 2026-09-23 "Log the book
  on each move", the read-only option of the three put to him). Covers
  were flipping price every minute or two (Texas governor dem 15c<->29c,
  Pennsylvania-01 rep 57c<->60c, Maine-02 dem entry 41c<->42c: each back
  at the first pass its cooldown allowed), and four tender entries
  filled unbooked while it moved them (House control rep 429 @ 10c
  "moved" after it filled, and a second bid of 483 rested on top for a
  minute). A rig reproduces the rhythm exactly when the book the tender
  reads does not show its own new order; with a book that does, the
  code holds steady. Two stamps could cause it and the saved state kept
  no books: the tender stamps its orders with the PASS's start
  (placed_ts=now) while _levels_net decides "already in the book" by
  placed_ts against the book's read; and the family and bonds stamp
  gateway books with their cycle's `now`, not the read. So every
  rested, moved, pull, filled, exit_filled, refused and move_refused
  line in the focus log now carries `diag`: the book's age and writer
  (stream or gateway), the side raw and as netted (DIAG_LEVELS), the
  other side's top, each tender order there with placed_vs_read,
  sent_vs_read (the clock just before the desk sent it, _sent_at) and
  whether the netting took it as in the book, and the live ghosts; a
  move adds why (bare, its own reading under FOCUS_KEEP of the plan's,
  the size or the intent), prev (the resting order's own score), was_d
  (the replaced order against the book it was judged on), desk (the
  desk's own words) and two; a pull adds prev and was_d. The page gets
  the lines without the books; the saved state keeps them. Nothing
  here feeds a decision — a failing diag writes its error and the pass
  goes on. The fix comes after the diag names the cause, with his yes.
  WHAT IT NAMED (the 00:20Z check of 2026-09-24): not the stamps. Every
  flip was judged on a stream book under 30 s old with our own order in
  it and netted right. The books moved: Pennsylvania-01 rep, someone's
  ~5,500 at 61c toggling that level 8.6k<->14.1k across the 10,000
  target, so our 60c cover fell in and out of the paid window (11 moves
  in 70 min at $0.002 a day each); Florida governor dem, ~4,700 coming
  and going at 26c with the 27c cover earning ~$1.00 a day against a
  ~$0.96 concession, so it read either side of zero (12 moves at
  $0.06-0.27 a day). FOCUS_KEEP alone moves an order for ANY gain when
  the readings sit near zero.

- A MOVE MUST BE WORTH SOMETHING, AND THE STOP GETS TIME TO SAVE
  (owner, 2026-09-24 "Yes, ship it" to both). (1) An order moves to a
  new price only when the new slot beats where it rests by
  FOCUS_MOVE_MIN_GAIN ($0.25 a day) as well as falling under FOCUS_KEEP;
  one past his fair with no company still moves at once, a wrong intent
  is still re-laid, and a RESIZE IS UNTOUCHED — he took this option, not
  the one that also held resizes under 25% (half of the hour's 64 moves
  were same-price resizes). The move line's why now says how far behind
  the new slot it was. (2) A ghost is not netted at a price where one of
  our own orders on the side rests now — there the ghost is the order it
  replaced, and netting it took others' size off twice (Pennsylvania-01
  at 23:09Z on 09-23: 6 shares off a level where only our 3 rested).
  (3) The stop save kept missing — the 09-22 22:48Z and 09-23 21:05Z
  deploys both booted from an older periodic save, with fix E in place
  for the second — because the launcher killed 3.0 fifteen seconds
  after passing on the stop while the save waited up to 5 s for each
  lock, took ~7 s to build and then uploaded. The launcher now gives it
  STOP_WAIT_S (45 s) and SHUTDOWN_LOCK_S is 2 s. The platform's own
  grace period still bounds it; the next boot's boot_restore says
  whether it landed.

- A COVER'S FILL IS BOOKED (owner, 2026-09-24 "Master off, then fix
  covers", after the repository review he asked for: "I'm feeling like
  I'm not in control of what's going on"). A cover buys a short back —
  intent SELL_SHORT, resting on the BID — so its fill RAISES the net.
  family.reconcile matched a vanished or shrunken order to the
  position's move by `intent == BUY_LONG`, which read every cover as a
  sale: in all three places (limbo, a shrunken size, an order gone from
  the list) a cover's real fill never matched, and a short that GREW
  could be booked as the cover filling. The cover went to limbo; the
  exchange's trade list or the hourly backfill recovered it, or the
  feed's "exchange wins" snap took the short off with no fill in the
  journal (48 BUY backfill rows of a share or more, 3,874 shares, in
  politics over the three days to 16:47Z, against 32 on the ask side).
  The tender's covers ride the same records, so its exit guard went
  unconfirmed too. Now _fill_sign takes the sign from rec.side — the
  test _on_fill books by — and nothing else changed. P21 grades it.
  Everything else from that review (one owner per market, the
  loss-takers off by default, sealing the pre-flag lots, the resize
  churn, limbo the tender can see, the step-up's bound) is NOT built and
  waits for his yes.

- A PUSH TO THE DEPLOY BRANCH IS THE DEPLOY, AND THE NEW COPY HOLDS
  (owner, 2026-09-24 "Build a boot hold"). DigitalOcean redeploys on
  every push to `deploy` (sync_deploy.yml says so): PR #331 merged at
  17:13:09Z and the new build restored its state at 17:14:39Z — before
  he could tap anything. So merging to deploy IS deploying: do it only
  on his yes. And the new container starts while the old one still
  runs; the old one is stopped only once the new one answers. Every
  deploy since fix D booted from "a periodic save" (09-22 22:48Z, 09-23
  21:05Z, 09-24 17:14:39Z from the 17:12:53Z save) because the new copy
  read the branch before the old copy's stop save could exist, and for
  the overlap both copies could trade, each reading the other's orders
  as his hand's. Now, in run() right after the web server starts and
  before the sampler, the tender, the stop handlers, the stream or any
  cycle: a boot whose container disk had no state (store.local_found
  False — a new container), that restored something other than a stop
  save, with a token to watch the branch, HOLDS (_boot_hold). It trades
  nothing, saves nothing, refuses every tap in handle_op with the
  minutes left, and shows "waiting for the old copy's final save" on
  the boot card, while it reads the branch head every BOOT_HOLD_POLL_S
  (10 s; one small ref read, the state downloaded only when the head
  moves). The old copy's stop save landing (is_stop_save, newer than
  what it restored) restarts the process IN PLACE (os.execve, same PID,
  so the launcher sees no exit; the disk is still empty, so the restart
  takes the branch's copy); with BOOT_HOLD_ENV set it never holds twice
  and boot_restore["after_hold"] says what it waited for. A newer
  periodic save and no stop save by BOOT_HOLD_S (300 s) is restored the
  same way; nothing newer and it goes on from what it has
  (boot_restore["hold"]). A launcher restart inside one container (disk
  has the state) never holds. The cost: up to five minutes of no
  trading and no taps on a new container that has no old copy (a
  platform restart), and the page is out for the seconds the in-place
  restart takes.
  CONFIRMED on its first deploy (merged 20:24:30Z, 2026-09-24, master
  on): the new copy held 63 s while the old one ran, the old copy's
  stop save (signal 15, build 3b2cdcf0) landed at 20:26:48Z, the hold
  restarted in place and restored it 10.5 s old ("the stop save",
  after_hold "the old copy's stop save, after 63s"), serving again at
  20:27:02Z. The overlap was real: a minute in which both copies would
  have traded. That stop save took the tender's lock but not the
  cycle's (cycle_lock false — a cycle was running; saved regardless).

- A MARKET'S FIRST DAY IN A PROGRAM COUNTS NOTHING ON THE METER (owner,
  2026-09-24 "Fix the meter's first day", after the report he asked
  for: which estimated markets the rewards endpoint never posted a row
  for, 09-10 to 09-21). Of $163.90 estimated on market-days that got no
  row on the four worst days, $137.44 was markets that JOINED a program
  partway through that ET day: all fifteen House district markets on
  09-11 (their program began 19:00Z, $44.56) and three 2028 markets
  that joined the boost at 16:48Z ($6.46), all eleven Senate combos on
  09-17 (read with no program the evening before, $84.49) — 29 of 30,
  while markets already in a program posted their first day, 18 of 18,
  and both groups posted every day after. So TermsStore now keeps
  empty_at (the last read that found no program, kept JOIN_EVIDENCE_S,
  36 h) and joined_at (JOIN_KEEP_S, 3 days): a program seen on a market
  that had none is a JOIN when a read found none within 36 h, or when
  the program itself began after that day's midnight ET (et_day_start);
  a market never read before, in a program older than today, gets the
  benefit of the doubt; a re-issue (one program replaced by another) is
  never a join. The tender's hand-off to the family's ledger goes
  through terms.adopt, the same rule. The 20-second sampler leaves a
  joined-today market's orders out of the meter until midnight ET
  (_first_day), logged "meter_first_day" once a market a day, and the
  state carries meter_first_day. METER ONLY: the tender and the engine
  still value those orders at the program's full rate that day — whether
  they should is his call and not built. Replayed on the saved ledger:
  the 42 House district markets seeded 09-11 are caught by the start
  rule, the twelve combos by the family's no-program read of 09-16.
  The rest of that report, for the record: the seat-count books (House
  seats R, Senate seats R) paid 0.88-1.18x the estimate 09-15..09-20 and
  then 0.37x on 09-21 (7 of 18 with no row) and 0.09x on 09-22 so far
  (12 of 20), while the House district markets on the same program paid
  1.1x and the program record read active at $600/day throughout, with
  16-23 of our orders resting there — cause NOT found; and 128 market-
  days worth $6.44 in all are pennies (cfb win totals, sub-$0.60 books).
  P22 predicts the demst seat markets that joined at 17:31Z on 09-22 get
  no 09-22 row and do get 09-23 rows.

- THE TIER ENGINES, STAGE 1: THE TIER FAIRS (owner, 2026-09-24/25: "We
  should build a different tender/engine for each tier" ... "No we're
  building from the ground up. It can borrow elements from the tender,
  but there were clearly flaws"; $1,000 of free cash assumed, a fixed
  share per tier reset daily from its EV, all switches off at start;
  "We have to get dynamic fairs. Things are changing fast";
  "Everything can go past fair if it is +ev"; decisions every second,
  "smart enough to spot when that is good vs bad. Hard coding rules is
  effective to a point but these rules are too easy to exploit"; and
  NOT one order a side — "it may be better to rest multiple orders at
  different price levels"). The design is v3/TIERS.md; four stages,
  each his yes, nothing trades before stage 4. A market's tier is its
  program's name (midterms_t1_..t4_), never its pay. Stage 1 is
  v3/tierfair.py, READ-ONLY: every TIERFAIR_TICK_S (1 s), on its own
  thread started after the boot hold, a fair for every tier market with
  a book less than 10 minutes old, from the book at depth (the first
  10% of the Target Size a side, at least 100 shares), the stream's
  last trade price (ws.Stream.last_trade; a first frame's price of
  unknown age is not a print), the linked markets (one less the other
  outcomes where every outcome of the event is here; an "at least N"
  ladder made to fall), and Silver — weighted by each input's measured
  error an hour ahead in its tier (equal until 30 readings), with a ±
  confidence. Graded every minute against the touch midpoint 10 minutes
  and an hour later, paired with the plain midpoint and his focus-page
  fair; a touch wider than 10c is not graded. /tiers (the tab after
  focus) shows it; state["tierfair"] keeps the grades. It reads no
  book of its own: Tier 4's 829 markets mostly have no stream seat, so
  few of them get a fair until seats are decided. P23 grades it.

- WHAT A POSITION HOLDS IS ITS SHARES TIMES THE PRICE (owner,
  2026-09-25 "Build the fix, ask before deploy"). The rule that a
  tender entry never adds to a position past the stake read what the
  position held from the exchange's cost field, and the feed reported
  Alaska governor (jonkre) short 90 at $0.00: the tender read "holding
  $0" and grew a short-opening ask beside it, 64 -> 127 shares at 71c
  one resize every ten minutes, past the $18 stake and the $46 growth
  ceiling, with $27 already at risk. At 13:55Z 21 held positions read
  $0 in the feed, and where it gave a number it was not the collateral
  either (ME Senate rep short 81 sold at 31c: $29.45, where 81 x 69c is
  $55.89). Focus._held now prices it: the family's own book of the lot
  when clean (not "est", a price between 0 and 1) — a long what it
  paid a share, a short a dollar less what it sold for — else the
  book's midpoint, else the feed's cost, else a dollar a share. The
  feed's cost is still what the page shows beside the position ("held"
  and "held_src" beside it). Replayed on the 13:55Z state: ten markets
  go from room to "no entry that adds to it" (Alaska, House seats
  >=180/205/235, Senate seats 49/52/54, TX-15 dem, AZ gov rep, NV gov
  dem) and four resting entries come off (Alaska 127 @ 71c, House
  >=205 60 @ 41c, >=235 32 @ 2c, >=180 2 @ 91c); Senate seats 50 goes
  the other way (feed $102, its fills $16).
- THE TIER FAIR STARTS FROM THE MIDPOINT (owner, 2026-09-25 "Start the
  fair from the midpoint"). The stage-1 fair, the inputs' average
  weighted by each one's own error, missed by 2.20c an hour ahead where
  the plain midpoint missed by 0.13c, better in 1 of 97 markets, with
  prices moving in 78 of them — P23 falsified. Now, where the touch is
  10c or narrower, the fair is the touch midpoint plus the average of
  each PROVEN input's share of its distance from it: per tier, a
  decaying least-squares tally (Beta) learns how much of that distance
  the midpoint covered an hour later, held between 0 and 1, 0 under
  BETA_MIN_N (30) readings and shrunk by n / (n + BETA_SHRINK_N, 100).
  An input that has proven nothing moves nothing. Wider than 10c there
  is no midpoint and the old weighted average stands. The fair's grade
  restarts under FAIR_VERSION "mid-2026-09-25" (a save without it drops
  only the "fair" errors); P24 grades it. Still read-only.
  ALL THREE GRADES RESTART TOGETHER (owner, 2026-09-25 "Restart all
  three"): dropping only the fair's errors left the midpoint's and his
  fair's carrying the morning's readings in Tiers 1-3, so P24 could
  compare the two only in Tier 4. FAIR_VERSION "mid-2026-09-25b": a save
  of any other version drops the fair's, the midpoint's and his fair's
  errors (tier and per-market) together (GRADED_TOGETHER); the inputs'
  errors and the learned shares are kept. P24's three days run from it.

- TIER 4 IS IDENTIFIED AND MONITORED, BOTH PROGRAMS (owner, 2026-09-25
  "Can you focus on getting the tier 4 markets identified and monitored
  both $5 and $2"). The ledger already held both: midterms_t4_house_
  winners ($5/day per event, 829 markets, event of 2) and politics_t4_
  coverage ($2/day per event, 5,009 markets — 3,803 House margin
  buckets, 337/330 governor/Senate margins, 316 turnout, 109 state
  House seat counts, the rest down-ballot), Target Size 2,000, discount
  0.4, all in the family's universe. What was missing was books: the
  two stream connections seat 400 markets and the tender's board takes
  most of them, and the gateway throttles. So: (1) tierfair.TIERS has a
  fifth tier, "t4c" (politics_t4_coverage_), beside "t4"; (2) Tier 4
  gets a stream of its own — T4_STREAM_SHARDS (2) connections of up to
  T4_PER_SHARD (3,000) markets, each in subscribe requests of
  T4_SUB_CHUNK (100; the exchange's docs: "a maximum of 100 markets per
  subscription. Use multiple subscriptions if you need more"), full
  book and Lite, debounced — writing into main.t4cache, a store ONLY
  the tier fairs read (TierFair._book takes the fresher of it and the
  family's cache). The engine, the tender and the meter never see it,
  and it never touches the gateway. A subscribe the exchange refuses is
  kept in its words (Stream.refused, status "refused") and shown on
  /tiers and in state["ws_t4"]; the two existing connections send
  exactly what they did. (3) Tier 4 is worked out every SLOW_EVERY_S
  (10 s), written down for grading every SLOW_SNAP_S (10 min), and each
  Tier 4 card lists SLOW_PAGE_ROWS (100) markets, the most recently
  traded first; the market ground, the event groups and their shapes
  are rebuilt every 10 s instead of every second. Measured on a rig of
  5,870 Tier 4 markets and 130 others: the per-second pass 3 ms median,
  the 10-second Tier 4 pass 127 ms, ~36 MB. Read-only: nothing places.
  TEN SUBSCRIPTIONS A CONNECTION (the first deploy, 16:32Z): each
  connection took its first ten subscribe requests and refused the rest
  — "max subscriptions per connection reached", a limit the docs never
  state — so 2,000 of the 5,838 had books (all 829 of the $5, 1,171 of
  the $2) and P25 was falsified. Owner ("200s on 3 connections"):
  T4_STREAM_SHARDS 3, T4_PER_SHARD 2,000, T4_SUB_CHUNK 200 (the size the
  main connections have always used, accepted), full books only
  (lite=False), and ws.WS_MAX_SUBS (10) caps what a connection sends —
  a slice too big for ten says so as "no_room" rather than being
  refused. P26 grades it.
  THE OLD ENGINE TAKES A TIER 4 BOOK FROM THAT STREAM (owner, 2026-09-25
  "Build it, ask before deploy"): at 17:40-17:52Z 24 of 38 throttled
  gateway reads were the engine fetching Tier 4 books the stream already
  carried. In _refresh_books — the held/active refresh and the candidate
  scan, the two places it reads the gateway — family._streamed hands it
  the monitor store's book instead when that book is at most
  STREAM_BOOK_MAX_S (60 s) old, well inside the desk's 120 s placement
  gate; older or not carried, the gateway read runs as before. The book
  keeps its own stamp; the scan still counts the candidate against its
  budget (the same 24 scored a cycle, fewer of them read through the
  gateway); and a book reaches the engine's cache, and so its fill
  model, only where it would have been read anyway — the store is NOT
  poured into the engine wholesale, which would have shifted the fill
  odds the tender's House markets share. ws_t4 carries engine_hits and
  engine_misses so a check can say how many reads it saved.

- THE TENDER READS THE EXCHANGE'S TRADE RECORD WHEN ITS ORDER SHRINKS
  (owner, 2026-09-26 "Yes, write it up" ... "Yes, build it"; NOT
  deployed without his separate yes). Five tender fills in 12 hours on
  09-26 were found only by the hourly match: the family books a fill
  when the position feed has moved with it, and the feed lags. Texas
  Senate dem, 05:25Z: 42 of a 90-share entry filled and ten seconds
  later the tender resized it back up to 79. Minnesota Senate dem,
  23:49Z 09-25: journaled twice, read as 230 of 115, the exit rested at
  230 for three minutes. House control rep, 04:17Z: an exit sold
  unbooked and a second exit rested on the closed position. Now:
  (1) the pass a tender order shrinks or leaves the book, the tender
  reads the exchange's trade record (recent_trades, the SIGNED account
  api, one try, 8 s, at most every FOCUS_RECORD_EVERY_S = 30 s and
  FOCUS_RECORD_TRIES = 4 reads an order) and the family books what the
  record names by order id at once (fam.book_record_fill, under the
  family's _fill_lock, which reconcile now takes too; the order leaves
  limbo so the feed's later move is not booked again; never more than
  the order lost that time); (2) while an entry's shrink is unexplained
  its side grows nothing — no new entry, no resize, no move — for up to
  FOCUS_VANISH_WAIT_S; a placement withdrawn because the list never
  showed it is checked but does not hold the side; (3) one trade is
  booked once: the hourly match leaves an execution of an order still
  on our books or in limbo to the live paths for RECORD_ADD_GRACE_S
  (900 s), the tender's orders count as ours there (not "your own
  trade"), and the tender counts a journal episode by its total, capped
  at what the order lost. Found while building it and fixed with it: the
  tender's "back at full size" test compared an order's remainder with
  what it LOST (48 left >= 42 filled read as a read that missed it), so
  a partial fill whose remainder was the larger part was dropped at
  once; it now compares with the size the order had. The gap left: an
  order the tender itself moves or pulls in the seconds before the
  family sees it shrink is still found only by the hourly match. P27
  grades it.
  DEPLOYED 14:36Z 2026-09-26 (owner "Deploy now"; PR #341, build
  f1d95da4, booted 14:38:50Z from the stop save after a 64 s hold). Its
  first booking came 20 s in: the Texas Senate dem cover of 41, filled
  at 14:36:29 during the restart, booked from the record at 14:39:10
  while the feed still read short 41 — the record was right.

- AFTER A BOOT THE TENDER READS ONE POSITION FEED (owner, 2026-09-26
  "Yes, build it"; NOT deployed without his separate yes). That same
  boot, the tender read the exchange's positions itself (boot_pos, flat)
  while the first cycle's guarded read kept Texas at its last value
  (short 41) because our log did not yet show it closed: the booked 41
  was added to the flat reading — long 41, an exit SALE of 41 at 67c
  (14:39:13) — then on the cycle's read it was short 41 and a cover of
  41 at 60c rested (14:39:57) on a position that was flat. It came off
  at 14:47:44; neither filled. Three parts: (1) _focus_pass works only
  from the cycle's read (_bond_positions, set after the families have
  reconciled against it) and does not run at all before the first
  cycle has one — no read of its own; the stream and the seed's market
  list are untouched, and the first pass waits for the first cycle (70
  s that boot); (2) an order of the tender's in the family's limbo
  (gone_pending) is taken as one that just left the book, once
  (_from_limbo), so its side holds and the record is asked about it —
  at a boot the first reconcile finds the restored orders gone before
  the tender ever saw them; (3) an exit booked by the journal moves the
  tender's view toward flat from what the feed shows NOW, never past
  it (_toward_flat), whatever the feed did before — a feed that already
  shows the fill leaves nothing to add. Entries keep their rule.

- THE TIER ENGINES, STAGE 2: THE VALUE (owner, 2026-09-26 "We're going
  to build things from the ground up. Focus on building and if you don't
  have something ask me to get it" — the $1,000 is assumed; nothing waits
  for the money). v3/tiervalue.py, READ-ONLY, on the tier fairs' thread:
  for every tier market with a book under five minutes old, each side and
  each candidate price (a tick inside the touch where the spread allows,
  the touch, three ticks behind, every resting level within 6c), a day:
  reward = our share of the side's pool by scoring.py's arithmetic (a side
  under its Target Size pays nobody; nothing on a market's first day in
  its program), less fills = fills a day x shares x the loss a share, less
  capital = collateral x his cost of capital (focus.coc_day, 0.5%). Fills
  a day are MEASURED on paper orders per tier, side, distance from the
  touch and line ahead, from a prior worth one day; the loss a share is
  what filled paper orders lost against the midpoint an hour later, per
  tier and side, pulled toward the market's own, never under his fill
  floor, plus his 2026-09-12 concession rule past the fair. Our own
  resting orders (the tender's, the engine's, the bonds') come out of the
  book first; his hand's stay in as company. The best set: $5 slices,
  each to the price that adds the most a dollar; the share saturates so a
  side stops by itself; a side under its Target Size may be carried over
  it in one step; NO CAP PER MARKET (owner, 2026-09-26 "No cap — the math
  decides", on the first plan's page: $1,000 netting $129.30 a day with $5
  of capital, "way too safe" — four Tier 2 and five Tier 3 markets sat at
  exactly the $100 a 10% cap of mine allowed, with money still worth more
  there, and Tier 1 got nothing). Each tier runs to the whole
  $1,000, so one pass gives each tier alone, the design's split (share =
  value alone over the sum) and the joint best split. Paper orders: one a
  side at its best price, followed an hour — filled when the other side
  reaches the price, a trade prints through it, or prints at it with
  nothing left ahead (the line ahead only shrinks); Tier 4's stream sends
  no prints, so there only the crossing counts and its fills are a floor.
  Graded: fills predicted vs seen per tier, the loss a share, and the
  meter's estimate vs the exchange's pay per tier since 09-24 (P28). Tier
  3's 54 markets now ride the Tier 4 monitor stream at its front (they
  lost their stream seats leaving the tender's board on 09-24). Measured
  on a rig of 6,000 markets: the Tier 1-3 plan 84 ms every 10 s, the
  Tier 4 plan 0.63 s a minute. NOT built: flickering levels counting for
  less, our own real fills grading the paper ones (stage 3).

- THE TIER ENGINES, STAGE 3: PAPER TRADING (owner, 2026-09-26 "We need to
  go back to building the bigger 4 tier earning machine"). v3/tierpaper.py,
  READ-ONLY, on the tier fairs' thread after stage 2: an engine per tier
  decides every second (Tier 4 every ten) what it would rest with its
  share of the $1,000 (split once an ET day at midnight, and at the first
  plan after a boot, by each tier's value alone over the sum), fills its
  paper orders off the real tape by stage 2's rule, holds the positions,
  rests one exit per position sized to what is held (a position's exit
  side takes no entry — the lot is never offered twice), and keeps books:
  reward on a TIME-AVERAGED book (others' size averaged over five minutes,
  so a level that comes and goes counts for the time it is there), capital
  at his 0.5% a day, realized fills, positions marked to the midpoint —
  against what stage 2 predicted for the same hours; each ET day written
  down at midnight. A side changes only when the gain a day over its
  measured holding time beats the actions it takes at an action's price;
  30 places and cancels a minute a tier, the best skipped move pricing
  the action when the cap binds. A tier never commits more than its money
  (positions count against it); a side the spread stops funding is given
  up when its money is wanted elsewhere. THE CHECK HE ASKED FOR ("How
  will you know how well it is estimating when everything is read only"):
  every real tender order gets a paper twin at its price, filled by the
  same tape rule, and is scored when the real order leaves the book (ten
  minutes' grace for its fill to be booked) or has rested six hours —
  both filled, only the twin, only the real order, neither (P29). No
  switches (it places nothing); the hand-over of markets from the tender
  and the old engine is stage 4.

## Evidence and predictions (owner, 2026-08-23)
- "We want verifiable and testable predictions and we want to keep
  getting closer to the goal of stable and high earnings."
- The pay page grades a day as it posts (owner, 2026-09-11 "For the
  paid/estimated number can you only consider the rows for markets
  that have been posted already? So I get a sense as I'm going how
  high or low I'm running"): the exchange posts a day market by
  market over hours, so beside the whole day's paid and estimate the
  ratio shown is paid over the estimate for the markets posted so
  far (the per-market claims in mkt_claim_day), with the count of
  estimated markets posted and what was paid on markets never
  estimated.
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
- The 2028 books are CLOSE-OUT ground (owner, 2026-09-12 "get out of
  2028 markets"; the same rail as DeSantis 2028 on 2026-08-27): every
  slug carrying "2028" is on the politics liquidate list — the engine
  sells the stock into the bid up to its shown size each cycle until
  flat, rests covers for the shorts, never buys, and pulls every
  order there that is not his hand's, a bond's or an exit; the focus
  tender drops the ground (a close-out market is never boosted for
  it). Sep 11 had paid $6.72 there against a $24.57 estimate while
  the day's 2028 round trips lost about $26. His hand orders there
  stay his. An order the open list still shows after WE cancelled it
  is ours still — cancelled again (once a minute, ten times at most),
  never adopted as his (2026-09-12, 09:44-09:46Z: the family
  cancelled the tender's fifteen leftover 2028 orders at boot, the
  list showed eight of them two minutes later, and they were recorded
  as his hand's — untouchable — while they rested on as the entries
  he had just asked out of); every desk remembers its cancels for a
  day, the memory is saved with the state, and a restart seeds it
  from each family's own log of orders it cancelled. On close-out
  ground nothing may ADD to what he asked out of, whoever placed it:
  a bid that is not a cover or a 1c wall is cancelled, an ask past
  the stock held (it opens a short) is cancelled, his 1c/99c walls
  and his asks of held stock stay (2026-09-12, 09:46-12:25Z: the old
  build's last 2028 entries came back after the deploy as "his
  hand's" — a 181 bid at 11c filled and was sold at 9c, a 2c ask
  opened a short, and a 333-share 6c bid blocked the close-out's own
  sale by self-match). And a close-out sale books only what the
  exchange's answer says executed: answered with no execution,
  nothing is sold and nothing is booked (11:35-12:22Z: the 28-share
  sale into that ghost bid was booked as sold twenty times).
- The cancel reason read is GONE (owner, 2026-09-12 "Do the cancel
  reason read", then "Do we even need cancel reason reads. I thought
  you said it wasn't doing anything" and "Delete the cancel reason
  read"): it looked up 26 orders that vanished from the open list and
  answered NOTHING — every one came back "not in the record" — because
  the exchange does not keep a cancel reason anywhere we can read it.
  Its activity feed carries ACTIVITY_TYPE_TRADE alone (2,479 of 2,500
  rows, the rest resolutions, transfers and cash moves; not one order
  row), so an order cancelled without trading never appears; and
  unsolicitedCancelReason is an EXECUTION field, not an order field,
  so it can only ever describe a trade. The 2026-08-23 probe that
  found "cancel reasons" had found them on executions, and I did not
  check which rows carried the field before building the read. It also
  did harm: both reads ran inside the family's cycle against the one
  endpoint that hangs, and with a 30-second timeout on four tries the
  lap went 4 s -> 125 s -> 1,177 s (2026-09-12, 20:25-20:52Z), so
  every exit, cancel and settle ran up to twenty minutes late. The
  queue, the counters, the activity-type log and both reads are
  deleted. What a vanished order still gets: the "silent_cancel" log
  line with its market, side, price and size. WHY THE BATCHES LEAVE is
  answered by experiment instead — all six batches on 2026-09-12 left
  within a minute of a fill with the account at its margin limit (his
  own diagnosis, "the buying power got too low for the size of the
  order"), and the $300 kept free now tests it: if the batches stop,
  the diagnosis is confirmed.
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
