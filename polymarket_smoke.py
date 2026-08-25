#!/usr/bin/env python3
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UA = "nanohit-polytest/0.1 github-actions"


def get_json(base, params=None, timeout=25):
    url = base + (("?" + urlencode(params, doseq=True)) if params else "")
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            ms = round((time.perf_counter() - t0) * 1000, 1)
            try:
                data = json.loads(raw)
            except Exception:
                data = None
            return {"ok": 200 <= r.status < 300, "status": r.status, "ms": ms, "url": url, "data": data, "body_prefix": raw[:800]}
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        return {"ok": False, "status": e.code, "ms": ms, "url": url, "data": data, "body_prefix": raw[:800]}
    except (URLError, TimeoutError, Exception) as e:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"ok": False, "status": None, "ms": ms, "url": url, "error": repr(e)}


def as_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "results", "trades", "markets", "activity", "positions"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def first(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def iso_date_from_event(ev):
    ts = first(ev, "timestamp", "time", "createdAt", "created_at")
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
            n = int(ts)
            if n > 10_000_000_000:
                n //= 1000
            return datetime.fromtimestamp(n, tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def summary_row(resp):
    return {k: resp.get(k) for k in ("ok", "status", "ms", "url", "error", "body_prefix") if k in resp}


def main():
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "checks": {},
        "wallet_checks": [],
    }

    # 1) Gamma public market discovery.
    gamma = get_json(
        "https://gamma-api.polymarket.com/markets",
        {"limit": 10, "active": "true", "closed": "false"},
    )
    report["checks"]["gamma_markets"] = summary_row(gamma)
    markets = as_items(gamma.get("data"))
    report["gamma_market_count"] = len(markets)
    condition_ids = [first(m, "conditionId", "condition_id") for m in markets]
    condition_ids = [x for x in condition_ids if x]
    report["sample_condition_ids"] = condition_ids[:3]

    # 2) Data API public trades. Start global: enough for connectivity + wallet discovery.
    trades = get_json(
        "https://data-api.polymarket.com/trades",
        {"limit": 500, "offset": 0, "takerOnly": "false"},
    )
    report["checks"]["trades"] = summary_row(trades)
    trade_items = as_items(trades.get("data"))
    report["trade_count"] = len(trade_items)

    wallets = []
    seen = set()
    for t in trade_items:
        w = first(t, "proxyWallet", "proxy_wallet", "user", "makerAddress", "maker_address")
        if isinstance(w, str) and w.startswith("0x") and w not in seen:
            seen.add(w)
            wallets.append(w)
    wallets = wallets[:20]
    report["candidate_wallets"] = wallets

    # 3) Public user endpoints for discovered wallets.
    rebate_event = None
    for w in wallets[:10]:
        activity = get_json(
            "https://data-api.polymarket.com/activity",
            {"user": w, "type": "MAKER_REBATE", "limit": 100, "offset": 0},
        )
        closed = get_json(
            "https://data-api.polymarket.com/closed-positions",
            {"user": w, "limit": 10, "offset": 0},
        )
        acts = as_items(activity.get("data"))
        cps = as_items(closed.get("data"))
        row = {
            "wallet": w,
            "activity_status": activity.get("status"),
            "activity_ms": activity.get("ms"),
            "rebate_activity_count": len(acts),
            "closed_positions_status": closed.get("status"),
            "closed_positions_ms": closed.get("ms"),
            "closed_positions_count": len(cps),
        }
        report["wallet_checks"].append(row)
        if acts and rebate_event is None:
            ev = acts[0]
            rebate_event = {
                "wallet": w,
                "date": iso_date_from_event(ev),
                "event": ev,
            }

    # 4) Point-lookup semantics of /rebates/current.
    # Prefer a wallet/date observed in MAKER_REBATE activity; otherwise probe recent dates.
    point_checks = []
    if rebate_event and rebate_event.get("date"):
        rb = get_json(
            "https://clob.polymarket.com/rebates/current",
            {"date": rebate_event["date"], "maker_address": rebate_event["wallet"]},
        )
        point_checks.append(summary_row(rb))
        report["rebate_activity_example"] = rebate_event
        report["rebate_lookup_rows"] = as_items(rb.get("data"))[:5]
    elif wallets:
        for days_ago in range(0, 7):
            d = (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()
            rb = get_json(
                "https://clob.polymarket.com/rebates/current",
                {"date": d, "maker_address": wallets[0]},
            )
            entry = summary_row(rb)
            entry["row_count"] = len(as_items(rb.get("data")))
            point_checks.append(entry)
            if entry["row_count"]:
                report["rebate_lookup_rows"] = as_items(rb.get("data"))[:5]
                break
    report["checks"]["rebates_current"] = point_checks

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = {
        "gamma_http": gamma.get("status"),
        "trades_http": trades.get("status"),
        "wallets_discovered": len(wallets),
        "activity_http_seen": sorted({x.get("activity_status") for x in report["wallet_checks"] if x.get("activity_status") is not None}),
        "closed_positions_http_seen": sorted({x.get("closed_positions_status") for x in report["wallet_checks"] if x.get("closed_positions_status") is not None}),
        "maker_rebate_activity_found": rebate_event is not None,
        "rebates_current_http_seen": sorted({x.get("status") for x in point_checks if x.get("status") is not None}),
    }

    with open("smoke_results.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print("RESULT_FILE=smoke_results.json")

    # Only fail CI if the two basic public API families are unreachable.
    if gamma.get("status") is None or trades.get("status") is None:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
