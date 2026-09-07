"""Places: the outbound addresses this server has run from, and what
the exchange made of each (owner, 2026-09-07: "make a way of tracking
which places Polymarket thinks are vpn and which are okay").

The exchange refuses placements from some addresses as "a VPN" (HTTP
403, code 7 — 2026-09-05) while taking cancels; the fix has been a
Deploy, which lands the container on a new outbound address. This
ledger remembers every address the host has been seen on, when, and
the exchange's verdict while it was current: placements accepted (okay)
or refused as a VPN. A deploy that lands on an address the exchange
last called a VPN is said so at once, before the first refusal.

The address is learned by asking a plain what-is-my-address service
(three of them, first answer wins) — hourly, at boot, right after a
refusal and right after a recovery. One honest limit: the host's
outbound pool may hand different connections different addresses, and
the exchange does not say which one it saw; a verdict is pinned to the
address the host showed at that time."""

from __future__ import annotations

import ipaddress
import time

CHECK_EVERY_S = 3600.0          # the routine look
RECHECK_AFTER_S = 300.0         # a refusal re-checks the address, at most this often
LOOKUPS = ("https://checkip.amazonaws.com",
           "https://api.ipify.org",
           "https://icanhazip.com")
KEEP_ADDRESSES = 60
KEEP_EVENTS = 40
LOOKUP_TIMEOUT_S = 5.0


def _fetch(url: str) -> str:
    import requests
    r = requests.get(url, timeout=LOOKUP_TIMEOUT_S,
                     headers={"User-Agent": "liquidity-rewards/3.0"})
    r.raise_for_status()
    return r.text


def parse_address(text: str) -> str | None:
    """The address in a lookup's answer, or None."""
    try:
        return str(ipaddress.ip_address((text or "").strip()))
    except ValueError:
        return None


class Places:
    def __init__(self, fetch=None, clock=None, on_change=None, lookups=LOOKUPS):
        self.fetch = fetch if fetch is not None else _fetch
        self._clock = clock if clock is not None else time.time
        self.on_change = on_change          # callable(ip, prior_verdict, meta)
        self.lookups = tuple(lookups)
        self.current: str | None = None
        self.checked_at: float = 0.0
        self.check_note: str = ""           # why the last look found nothing
        self.recheck_at: float = 0.0
        self.seen: dict[str, dict] = {}     # ip -> the record
        self.events: list[dict] = []

    # ------------------------------------------------------------ the look

    def due(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        return now - self.checked_at >= CHECK_EVERY_S

    def check(self, now: float | None = None, why: str = "hourly") -> str | None:
        """Ask what address the host shows; record it. Returns the
        address, or None when no lookup answered."""
        now = self._clock() if now is None else now
        self.checked_at = now
        ip = None
        errs = []
        for url in self.lookups:
            try:
                ip = parse_address(self.fetch(url))
            except Exception as e:  # noqa: BLE001 — a lookup is a bonus
                errs.append(f"{url.split('//')[-1].split('/')[0]}: {str(e)[:60]}")
                continue
            if ip:
                break
            errs.append(f"{url.split('//')[-1].split('/')[0]}: not an address")
        if ip is None:
            self.check_note = "; ".join(errs)[:200]
            self._event(now, "lookup_failed", ip=self.current, why=why,
                        note=self.check_note)
            return None
        self.check_note = ""
        m = self.seen.get(ip)
        new = m is None
        if new:
            m = {"first": round(now, 1), "last": round(now, 1), "checks": 0,
                 "accepted": 0, "refused": 0, "last_ok": 0.0, "last_vpn": 0.0,
                 "boots": 0}
            self.seen[ip] = m
        m["last"] = round(now, 1)
        m["checks"] += 1
        if why == "boot":
            m["boots"] += 1
        if ip != self.current:
            prior = "new" if new else self.verdict(ip)
            was = self.current
            self.current = ip
            self._event(now, "address", ip=ip, why=why, prior=prior, was=was,
                        note=(f"{'a new address' if new else 'seen before: ' + prior}"
                              + (f", was {was}" if was else "")))
            if self.on_change is not None:
                self.on_change(ip, prior, dict(m))
        self._trim()
        return ip

    # ------------------------------------------------------------ verdicts

    def verdict(self, ip: str | None = None) -> str:
        """okay / vpn / new for an address, by the exchange's last word."""
        m = self.seen.get(ip or self.current or "")
        if not m:
            return "new"
        ok, bad = float(m.get("last_ok") or 0.0), float(m.get("last_vpn") or 0.0)
        if not ok and not bad:
            return "new"
        return "vpn" if bad > ok else "okay"

    def refused(self, now: float | None = None) -> None:
        """The exchange refused a placement as a VPN: pin it to the
        address the host shows — looked up again first, so a rotated
        pool does not get the wrong address blamed."""
        now = self._clock() if now is None else now
        if now >= self.recheck_at:
            self.recheck_at = now + RECHECK_AFTER_S
            self.check(now, why="refused")
        m = self.seen.get(self.current or "")
        if m is None:
            return
        first = self.verdict(self.current) != "vpn"
        m["refused"] += 1
        m["last_vpn"] = round(now, 1)
        if first:
            self._event(now, "vpn", ip=self.current,
                        note="the exchange refused a placement as a VPN")

    def accepted(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        m = self.seen.get(self.current or "")
        if m is None:
            return
        first = self.verdict(self.current) != "okay"
        m["accepted"] += 1
        m["last_ok"] = round(now, 1)
        if first:
            self._event(now, "okay", ip=self.current,
                        note="the exchange accepted a placement")

    def recovered(self, now: float | None = None) -> None:
        """Placements are accepted again after a refusal: look again,
        so the ledger shows which address they came back on."""
        now = self._clock() if now is None else now
        self.check(now, why="recovered")
        self.accepted(now)

    # ------------------------------------------------------------ the page

    def view(self) -> dict:
        rows = []
        for ip, m in self.seen.items():
            rows.append({"ip": ip, "verdict": self.verdict(ip), "current": ip == self.current,
                         **{k: m.get(k, 0) for k in ("first", "last", "checks", "accepted",
                                                      "refused", "last_ok", "last_vpn",
                                                      "boots")}})
        rows.sort(key=lambda r: (-int(r["current"]), -float(r["last"] or 0.0)))
        return {"current": self.current, "verdict": self.verdict(),
                "checked_at": self.checked_at, "check_note": self.check_note,
                "okay_n": sum(1 for r in rows if r["verdict"] == "okay"),
                "vpn_n": sum(1 for r in rows if r["verdict"] == "vpn"),
                "rows": rows, "events": list(self.events[-12:])}

    def to_dict(self) -> dict:
        return {"current": self.current, "checked_at": self.checked_at,
                "seen": self.seen, "events": self.events[-KEEP_EVENTS:]}

    def restore(self, d: dict) -> None:
        if not isinstance(d, dict):
            return
        self.seen = {str(k): dict(v) for k, v in (d.get("seen") or {}).items()
                     if isinstance(v, dict)}
        self.events = list(d.get("events") or [])
        cur = d.get("current")
        self.current = str(cur) if cur else None
        # a restart may be a new container: the first look is not "hourly"
        self.checked_at = 0.0

    # ------------------------------------------------------------ plumbing

    def _event(self, now: float, event: str, **kw) -> None:
        self.events.append({"ts": round(now, 1), "event": event, **kw})
        del self.events[:-KEEP_EVENTS]

    def _trim(self) -> None:
        if len(self.seen) <= KEEP_ADDRESSES:
            return
        keep = sorted(self.seen, key=lambda k: -float(self.seen[k].get("last") or 0.0))
        for k in keep[KEEP_ADDRESSES:]:
            if k != self.current:
                self.seen.pop(k, None)
