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
  around them so shares are never offered twice.
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
  exits pulled first. Nothing else may cross.
- NO SCHEDULED GitHub Actions (owner, 2026-08-24: "remove the GitHub
  automation. It keeps running and I keep getting emails"). Every cron
  workflow is deleted; the monitor writes rewards.csv, fills.csv,
  trades.csv, estimates.csv, the Silver tables and STATUS.md itself.
  The remaining workflows are manual-dispatch or push-path only and
  never fire on their own. Do not add a cron workflow — put the work
  in the monitor's publish loop instead.
- Alerts go through ntfy; the topic name is a password.

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
