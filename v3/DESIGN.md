# Version 3.0 — build brief

Written 2026-08-20, on the owner's instruction:

> "Take the best parts from each version. Simplicity of v2 and personality
> of v1. Make a new version that prioritizes politics but can be expanded
> in the way I expanded V2. Give me a new app."

Read `CLAUDE.md` first (standing preferences), then `REBUILD.md` (the 1.0
post-mortem — every lesson in it still binds). This file is only what 3.0
does differently from 2.0.

## What each version contributes

**From 2.0, the shape.** Small modules, one job each. The family pattern:
a market category is a `FamilyConfig`, and adding one is writing a config
function and flipping its own switch — that is how the owner expanded 2.0
to college football and the NFL in a day. The order rails (post-only,
place-verify-cancel, never /modify), the patient 429 discipline, the
terms-as-tracked-data store, the state branch, the tests. These are ported
nearly verbatim: `intents scoring programs api orders books alerts state
switch terms`.

**From 1.0, the voice and the politics brains.** Plain-English verdicts on
every order and every skipped market. A phone front page that leads with
✅/❌ freshness and the money number. Market NAMES everywhere (the feed's
own titles, decoded slugs as fallback) — never a bare slug on a page.
Politics discovery from the events feed, which carries the event pool
divisor with it. The estimate-integrity rule learned 2026-08-20: **no
estimate until the divisor is confirmed** — a market discovered outside an
event never shows a dollar figure.

## What 3.0 changes on purpose

* **One engine.** 2.0 ended up with two (the seats engine and the family
  engine). 3.0 has exactly one `Family`; politics is its first and
  biggest family. The seats/Silver EV machinery stays in 2.0 — it is a
  different business (holding positions on a model) from resting for
  rewards.
* **Politics first.** The politics family gets the book budget, the
  capital, and the front page. Expansion families get modest defaults.
* **Discovery is pluggable.** A family's universe is either slug prefixes
  (football-style, terms from a sweep) or a discover function (politics:
  the events feed by tag, names + event divisor included, econ markets
  refused at the door).
* **Resting style per family.** Politics is known ground: join the touch,
  but only in QUIET books (the cache's volatility EWMA is the gate);
  step back when the book is busy. Every NEW family is behind-the-touch
  only (owner, 2026-08-20) — that is the default; politics opts out.
* **Qualifying is just another candidate.** When a side is below Target
  Size, the planner may propose reviving it (fill the gap, own the side)
  — priced at the deep end, inside the same caps as everything else.
  Known-ground families only. This replaces 1.0's bolted-on qualifier
  loop and queue (REBUILD asked for exactly this).
* **One risk number per family** (`capital_usd`) and one line showing it.
  Per-market caps exist but the family ceiling binds.
* **The focus tender** (2026-09-10, v3/focus.py). The boosted politics
  markets are not the family engine's: a loop of its own every 15 s,
  outside the family cycle, tends them from the owner's fair prices by
  expected value (reward claim − fill odds × fill cost − cost of
  capital), one order a side, under a $1,000 expected-loss cap. The
  family sees that ground as frozen; the bonds hand it over. Every
  order there is the owner's to move from the /focus page.
* **Dead market = leave entirely.** Program gone or pool zero: cancel
  every order we have there, exits included, and the seller stands down
  (owner, 2026-08-20: "I don't want to be in markets if there are no
  rewards" / "You can remove the unwinding positions as well").
* **One earned-today number** per family: the live rate integrated over
  time, accruing only while book coverage clears a quorum. No correction
  factor, ever.

## Coexistence and the floor (the cutover mechanism)

1.0 and 2.0 keep running and earning; 3.0 boots alongside them (launcher
runs three processes; the front door proxies `/v3/*`). Every 3.0 switch is
OFF until the owner arms it, and until then 3.0 runs read-only: it
discovers, scores, and shows what it WOULD do.

The cutover is one switch, not a checklist (owner, 2026-08-20: "when I
turn on v3, the other versions will immediately halt before anything v3
related happens"). The mechanism is the FLOOR — v3/floor.py:

* 3.0's master switch ON writes a floor request file. 1.0 and 2.0 read
  it every loop, halt every automated order-touching loop (their own
  switches untouched), and write acknowledgements.
* 3.0 takes no order-touching action until BOTH acknowledgements are in
  and fresh. Armed-but-unacknowledged shows as "waiting for the floor".
* Once acknowledged, each armed family ADOPTS the account's resting
  orders on its ground — owner-placed manual orders excepted — takes
  long stock onto its exit seller, and maintains the inherited book at
  its usual measured pace (adoption starts each order's cooldown).
* Master OFF hands the floor straight back; 1.0/2.0 resume under their
  own switches.

2.0 keeps the published-rewards watcher and 1.0 keeps the tracker,
STATUS.md, the front door, and the owner's manual map controls — those
run with or without the floor.

## Standing rules (unchanged, non-negotiable)

Never econ markets. Secrets only in env. Order-touching keeps auth +
X-Reprice CSRF + whitelist + 0.1–99.9c bounds + post-only. Nothing places
without its family's owner switch (off by default, two taps on, one tap
off, every flip logged and pushed). Never call /modify — place, verify by
order id and quantity, then cancel. Fills are usually losses: rest where
fills are unlikely, prefer low-volatility books near the touch.
