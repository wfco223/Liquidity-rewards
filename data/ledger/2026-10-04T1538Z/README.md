# Transaction record, pulled 2026-10-04T15:38:54Z

✅ Complete: both walks reached the end of the exchange's record.

Pulled from the exchange's own API by a read-only run. Nothing in this folder is ever edited after it is written; a later pull goes in a new folder, and the commit is tagged `ledger-2026-10-04T1538Z`.

## Activities (trades, settlements, cash moves — every type)
- 14,297 rows, 2025-12-16T21:32:03.613120005Z to 2026-10-04T14:44:06.618669744Z (UTC)
- by type: ACTIVITY_TYPE_ACCOUNT_DEPOSIT 44, ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL 42, ACTIVITY_TYPE_POSITION_RESOLUTION 76, ACTIVITY_TYPE_REFERRAL_BONUS 3, ACTIVITY_TYPE_TRADE 14,042, ACTIVITY_TYPE_TRANSFER 90
- 143 pages read; 0 repeated rows dropped by id
- stopped: the exchange said eof

## Reward payouts
- 13,068 rows, 2026-07-01 to 2026-10-02; PAID rows add up to $17,509.64
- asked from 2025-01-01 on https://api.polymarket.us; 1 pages
- stopped: no further page token

## What was held at the pull
- positions: 496 markets read; balances: read

## Files
- `activities-NNNN.jsonl.gz`: every activity row exactly as the exchange sent it, newest first, 2,000 to a file
- `activities.csv`: one readable line per activity, oldest first. For a trade, `our_side` is the side the exchange's isAggressor flag names; `price` and `shares` are that side's execution
- `earnings.jsonl.gz` / `earnings.csv`: reward payout rows
- `positions.json.gz`, `balances.json`: what was held at the pull
- `manifest.json`: counts, ranges, why each walk stopped, and a sha256 of every file

| file | rows | bytes |
|---|---:|---:|
| activities-0001.jsonl.gz | 2000 | 984,076 |
| activities-0002.jsonl.gz | 2000 | 1,011,151 |
| activities-0003.jsonl.gz | 2000 | 1,013,233 |
| activities-0004.jsonl.gz | 2000 | 858,345 |
| activities-0005.jsonl.gz | 2000 | 948,093 |
| activities-0006.jsonl.gz | 2000 | 807,489 |
| activities-0007.jsonl.gz | 2000 | 1,003,701 |
| activities-0008.jsonl.gz | 297 | 121,113 |
| activities.csv | 14297 | 5,229,145 |
| earnings.jsonl.gz | 13068 | 93,174 |
| earnings.csv | 13068 | 966,524 |
| positions.json.gz |  | 42,642 |
| balances.json |  | 559 |
