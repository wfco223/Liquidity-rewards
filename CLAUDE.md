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
  that one order and runs the sale. Never without the yes.
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
- The market survey is GONE (owner, 2026-09-10 "Take off the survey
  entirely. Save the data on GitHub but i don't need it"): nothing
  samples, nothing enumerates the exchange's programs, no survey tab,
  no survey state. Its last table is
  data/survey_prefix_stats_2026-09-10.csv (with the raw .json beside
  it) and the older rows are data/survey.csv. v3/survey.py stays only
  for the shared math (walls, collateral, live-event checks). Do not
  bring the survey back without the owner's yes.

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
