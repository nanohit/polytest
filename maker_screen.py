#!/usr/bin/env python3
"""Cheap Phase -1 screen for Polymarket rebate-active makers.

Purpose: try to KILL the generic rebate-aware market-making thesis before
building an OrderFilled decoder or L2 research stack.

This is intentionally a wallet-level screen, not maker-fill attribution.
A negative result is strong evidence against the thesis; a positive result
only justifies deeper maker-fill reconstruction.
"""

import csv
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA = "nanohit-polytest-maker-screen/0.2 github-actions"
LOOKBACK_DAYS = 60
TARGET_MAKERS = 50
CANDIDATE_POOL = 300
ACTIVITY_CANDIDATES_TO_SCORE = 160
TRADES_PAGES = 8
TRADES_LIMIT = 1000
MIN_REBATE_DAYS = 2
MIN_MAKER_CONDITIONS_EVAL = 3
MIN_CONDITION_COVERAGE_EVAL = 0.70
REQUEST_PAUSE = 0.015


def fnum(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def first(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def as_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "results", "trades", "markets", "activity", "positions"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def ts_seconds(ev):
    x = first(ev, "timestamp", "time", "createdAt", "created_at")
    if x is None:
        return None
    try:
        if isinstance(x, (int, float)) or (isinstance(x, str) and x.isdigit()):
            n = int(x)
            if n > 10_000_000_000:
                n //= 1000
            return n
        return int(datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def event_date(ev):
    t = ts_seconds(ev)
    if t is None:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()


def request_json(base, params=None, timeout=30, retries=5):
    url = base + (("?" + urlencode(params, doseq=True)) if params else "")
    last = None
    for attempt in range(retries):
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        t0 = time.perf_counter()
        try:
            with urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                except Exception:
                    data = None
                time.sleep(REQUEST_PAUSE)
                return {
                    "ok": 200 <= r.status < 300,
                    "status": r.status,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "url": url,
                    "data": data,
                    "body_prefix": raw[:500],
                }
        except HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except Exception:
                data = None
            last = {"ok": False, "status": e.code, "url": url, "data": data, "body_prefix": raw[:500]}
            if e.code == 429:
                retry_after = None
                if isinstance(data, dict):
                    retry_after = data.get("retry_after_seconds") or data.get("retryAfterSeconds")
                if retry_after is None:
                    retry_after = e.headers.get("Retry-After")
                try:
                    wait = min(30.0, max(0.5, float(retry_after)))
                except Exception:
                    wait = min(30.0, 1.5 * (2 ** attempt))
                print(f"429: sleeping {wait:.2f}s for {base}", flush=True)
                time.sleep(wait)
                continue
            if 500 <= e.code < 600:
                time.sleep(min(10.0, 1.0 * (2 ** attempt)))
                continue
            return last
        except (URLError, TimeoutError, Exception) as e:
            last = {"ok": False, "status": None, "url": url, "error": repr(e)}
            time.sleep(min(10.0, 1.0 * (2 ** attempt)))
    return last or {"ok": False, "status": None, "url": url, "error": "unknown"}


def quantile(values, q):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def get_recent_trade_candidates(errors):
    stats = defaultdict(lambda: {"trades": 0, "volume_proxy": 0.0})
    rows_seen = 0
    for page in range(TRADES_PAGES):
        r = request_json(
            "https://data-api.polymarket.com/trades",
            {"limit": TRADES_LIMIT, "offset": page * TRADES_LIMIT, "takerOnly": "false"},
        )
        if r.get("status") != 200:
            errors.append({"stage": "trades", "page": page, "response": r})
            break
        rows = as_items(r.get("data"))
        rows_seen += len(rows)
        for t in rows:
            w = first(t, "proxyWallet", "proxy_wallet", "user", "makerAddress", "maker_address")
            if not (isinstance(w, str) and w.startswith("0x")):
                continue
            s = fnum(first(t, "size", "shares", "amount"))
            p = fnum(first(t, "price"), 1.0)
            stats[w]["trades"] += 1
            stats[w]["volume_proxy"] += abs(s * p)
        print(f"trade discovery page={page} rows={len(rows)} unique_wallets={len(stats)}", flush=True)
        if len(rows) < TRADES_LIMIT:
            break
    ranked = sorted(stats.items(), key=lambda kv: (kv[1]["trades"], kv[1]["volume_proxy"]), reverse=True)
    return ranked[:CANDIDATE_POOL], rows_seen


def get_rebate_activity(wallet, start_ts, end_ts, errors, full=False):
    events = []
    page_limit = 100
    max_pages = 12 if full else 2
    for page in range(max_pages):
        params = {
            "user": wallet,
            "type": "MAKER_REBATE",
            "limit": page_limit,
            "offset": page * page_limit,
            "start": start_ts,
            "end": end_ts,
        }
        r = request_json("https://data-api.polymarket.com/activity", params)
        if r.get("status") != 200:
            errors.append({"stage": "activity", "wallet": wallet, "page": page, "response": r})
            break
        rows = as_items(r.get("data"))
        for ev in rows:
            t = ts_seconds(ev)
            if t is None or (start_ts <= t <= end_ts):
                events.append(ev)
        if len(rows) < page_limit:
            break
        # If endpoint ignored start/end, stop once oldest event is older than lookback.
        known_ts = [ts_seconds(x) for x in rows if ts_seconds(x) is not None]
        if known_ts and min(known_ts) < start_ts:
            break
    return events


def score_candidate_makers(candidates, start_ts, end_ts, errors):
    scored = []
    for i, (wallet, trade_stat) in enumerate(candidates[:ACTIVITY_CANDIDATES_TO_SCORE], 1):
        events = get_rebate_activity(wallet, start_ts, end_ts, errors, full=False)
        dates = sorted({d for d in (event_date(e) for e in events) if d})
        rebate_activity_usdc = sum(fnum(first(e, "usdcSize", "usdc_size", "amount", "size")) for e in events)
        if dates:
            scored.append({
                "wallet": wallet,
                "rebate_days_preview": len(dates),
                "rebate_activity_usdc_preview": rebate_activity_usdc,
                "candidate_trades": trade_stat["trades"],
                "candidate_volume_proxy": trade_stat["volume_proxy"],
            })
        if i % 20 == 0:
            print(f"activity scored={i} rebate_active={len(scored)}", flush=True)
    scored.sort(key=lambda x: (x["rebate_activity_usdc_preview"], x["rebate_days_preview"], x["candidate_trades"]), reverse=True)
    return scored[:TARGET_MAKERS]


def get_rebate_rows_for_wallet(wallet, active_dates, errors):
    rows = []
    for d in sorted(active_dates):
        r = request_json(
            "https://clob.polymarket.com/rebates/current",
            {"date": d, "maker_address": wallet},
        )
        if r.get("status") != 200:
            errors.append({"stage": "rebates_current", "wallet": wallet, "date": d, "response": r})
            continue
        items = as_items(r.get("data"))
        for x in items:
            y = dict(x) if isinstance(x, dict) else {"raw": x}
            y["lookup_date"] = d
            y["wallet"] = wallet
            rows.append(y)
    return rows


def get_closed_positions(wallet, errors):
    all_rows = []
    limit = 50
    for page in range(200):
        r = request_json(
            "https://data-api.polymarket.com/closed-positions",
            {"user": wallet, "limit": limit, "offset": page * limit},
        )
        if r.get("status") != 200:
            errors.append({"stage": "closed_positions", "wallet": wallet, "page": page, "response": r})
            break
        rows = as_items(r.get("data"))
        all_rows.extend(rows)
        if len(rows) < limit:
            break
        if (page + 1) * limit >= 10000:
            errors.append({"stage": "closed_positions", "wallet": wallet, "error": "offset cap reached"})
            break
    return all_rows


def aggregate_wallet(wallet_info, activity_events, rebate_rows, closed_rows):
    # Exact maker conditions from positive /rebates/current rows.
    rebate_by_condition = defaultdict(float)
    rebate_total = 0.0
    for r in rebate_rows:
        cid = first(r, "condition_id", "conditionId")
        amt = fnum(first(r, "rebated_fees_usdc", "rebatedFeesUsdc", "earnings"))
        if cid and amt > 0:
            rebate_by_condition[str(cid)] += amt
            rebate_total += amt

    closed_by_condition = defaultdict(lambda: {"realized_pnl": 0.0, "estimated_cost": 0.0, "rows": 0})
    for p in closed_rows:
        cid = first(p, "conditionId", "condition_id")
        if not cid:
            continue
        realized = fnum(first(p, "realizedPnl", "realized_pnl"))
        total_bought = fnum(first(p, "totalBought", "total_bought"))
        avg_price = fnum(first(p, "avgPrice", "avg_price"))
        closed_by_condition[str(cid)]["realized_pnl"] += realized
        closed_by_condition[str(cid)]["estimated_cost"] += abs(total_bought * avg_price)
        closed_by_condition[str(cid)]["rows"] += 1

    maker_conditions = set(rebate_by_condition)
    closed_maker_conditions = maker_conditions.intersection(closed_by_condition)
    realized_pnl = sum(closed_by_condition[c]["realized_pnl"] for c in closed_maker_conditions)
    estimated_cost = sum(closed_by_condition[c]["estimated_cost"] for c in closed_maker_conditions)
    rebate_on_closed = sum(rebate_by_condition[c] for c in closed_maker_conditions)
    net_closed = realized_pnl + rebate_on_closed
    condition_coverage = (len(closed_maker_conditions) / len(maker_conditions)) if maker_conditions else 0.0
    rebate_coverage = (rebate_on_closed / rebate_total) if rebate_total > 0 else 0.0
    net_return_proxy = (net_closed / estimated_cost) if estimated_cost > 0 else None

    dates = sorted({d for d in (event_date(e) for e in activity_events) if d})
    activity_sum = sum(fnum(first(e, "usdcSize", "usdc_size", "amount", "size")) for e in activity_events)

    return {
        "wallet": wallet_info["wallet"],
        "candidate_trades": wallet_info.get("candidate_trades", 0),
        "candidate_volume_proxy": wallet_info.get("candidate_volume_proxy", 0.0),
        "rebate_days": len(dates),
        "rebate_activity_usdc": activity_sum,
        "rebate_exact_usdc": rebate_total,
        "maker_conditions": len(maker_conditions),
        "closed_maker_conditions": len(closed_maker_conditions),
        "condition_coverage": condition_coverage,
        "rebate_coverage": rebate_coverage,
        "realized_pnl_closed_maker_conditions": realized_pnl,
        "rebate_closed_conditions": rebate_on_closed,
        "net_pnl_screen": net_closed,
        "estimated_cost_proxy": estimated_cost,
        "net_return_proxy": net_return_proxy,
        "closed_positions_rows": len(closed_rows),
        "evaluable": (
            len(maker_conditions) >= MIN_MAKER_CONDITIONS_EVAL
            and condition_coverage >= MIN_CONDITION_COVERAGE_EVAL
            and estimated_cost > 0
        ),
    }


def write_csv(path, rows):
    if not rows:
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        return
    keys = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def main():
    started = datetime.now(timezone.utc)
    end_ts = int(started.timestamp())
    start_ts = int((started - timedelta(days=LOOKBACK_DAYS)).timestamp())
    errors = []

    print("PHASE -1 MAKER SCREEN", flush=True)
    print(f"lookback_days={LOOKBACK_DAYS} target_makers={TARGET_MAKERS}", flush=True)

    candidates, trade_rows_seen = get_recent_trade_candidates(errors)
    print(f"candidate wallets={len(candidates)} from trade_rows={trade_rows_seen}", flush=True)
    maker_preview = score_candidate_makers(candidates, start_ts, end_ts, errors)
    print(f"selected rebate-active makers={len(maker_preview)}", flush=True)

    wallet_rows = []
    all_rebate_rows = []
    all_activity_rows = []
    selected_details = []

    for idx, wi in enumerate(maker_preview, 1):
        wallet = wi["wallet"]
        activity = get_rebate_activity(wallet, start_ts, end_ts, errors, full=True)
        active_dates = sorted({d for d in (event_date(e) for e in activity) if d})
        # Require at least a little persistence, but keep all selected rows in output.
        rebate_rows = get_rebate_rows_for_wallet(wallet, active_dates, errors)
        closed = get_closed_positions(wallet, errors)
        row = aggregate_wallet(wi, activity, rebate_rows, closed)
        wallet_rows.append(row)

        for ev in activity:
            all_activity_rows.append({
                "wallet": wallet,
                "date": event_date(ev),
                "timestamp": ts_seconds(ev),
                "usdcSize": first(ev, "usdcSize", "usdc_size", "amount", "size"),
                "conditionId": first(ev, "conditionId", "condition_id"),
                "type": first(ev, "type"),
            })
        for rr in rebate_rows:
            all_rebate_rows.append({
                "wallet": wallet,
                "lookup_date": rr.get("lookup_date"),
                "condition_id": first(rr, "condition_id", "conditionId"),
                "rebated_fees_usdc": first(rr, "rebated_fees_usdc", "rebatedFeesUsdc", "earnings"),
                "asset_address": first(rr, "asset_address", "assetAddress"),
            })

        selected_details.append({
            "wallet": wallet,
            "active_dates": active_dates,
            "rebate_rows_count": len(rebate_rows),
            "closed_positions_count": len(closed),
        })
        print(
            f"maker {idx:02d}/{len(maker_preview)} {wallet[:10]} days={row['rebate_days']} "
            f"conditions={row['maker_conditions']} coverage={row['condition_coverage']:.2f} "
            f"net={row['net_pnl_screen']:.2f}",
            flush=True,
        )

    # Evaluable cohort: conditions visible in both exact rebate lookup and closed positions.
    evaluable = [r for r in wallet_rows if r["evaluable"] and r["rebate_days"] >= MIN_REBATE_DAYS]
    returns = [r["net_return_proxy"] for r in evaluable if r["net_return_proxy"] is not None]
    nets = [r["net_pnl_screen"] for r in evaluable]
    raw_realized = [r["realized_pnl_closed_maker_conditions"] for r in evaluable]

    summary = {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "trade_rows_seen": trade_rows_seen,
        "candidate_wallets": len(candidates),
        "rebate_active_selected": len(wallet_rows),
        "evaluable_wallets": len(evaluable),
        "errors": len(errors),
        "net_return_proxy": {
            "p10": quantile(returns, 0.10),
            "p25": quantile(returns, 0.25),
            "median": quantile(returns, 0.50),
            "p75": quantile(returns, 0.75),
            "p90": quantile(returns, 0.90),
        },
        "net_pnl_screen_usdc": {
            "p10": quantile(nets, 0.10),
            "p25": quantile(nets, 0.25),
            "median": quantile(nets, 0.50),
            "p75": quantile(nets, 0.75),
            "p90": quantile(nets, 0.90),
            "sum": sum(nets),
        },
        "realized_pnl_before_rebate_usdc": {
            "median": quantile(raw_realized, 0.50),
            "sum": sum(raw_realized),
        },
        "total_exact_rebates_usdc_selected": sum(r["rebate_exact_usdc"] for r in wallet_rows),
        "median_condition_coverage": quantile([r["condition_coverage"] for r in wallet_rows], 0.5),
        "median_rebate_coverage": quantile([r["rebate_coverage"] for r in wallet_rows], 0.5),
        "interpretation": {
            "negative_result_strength": "High: wallet-level profitability remains negative despite mixing maker+taker/directional activity.",
            "positive_result_strength": "Weak: positive wallet PnL does not prove maker fills themselves are profitable.",
        },
    }

    med = summary["net_return_proxy"]["median"]
    p75 = summary["net_return_proxy"]["p75"]
    if len(evaluable) >= 20 and med is not None and p75 is not None and med < 0 and p75 <= 0:
        verdict = "KILL_SIGNAL"
    elif len(evaluable) >= 20 and med is not None and med < 0:
        verdict = "NEGATIVE_SIGNAL"
    elif len(evaluable) < 20:
        verdict = "INSUFFICIENT_COVERAGE"
    else:
        verdict = "NO_KILL__DEEPER_ATTRIBUTION_REQUIRED"
    summary["verdict"] = verdict

    wallet_rows.sort(key=lambda r: (r["evaluable"], r["net_return_proxy"] if r["net_return_proxy"] is not None else -999), reverse=True)
    write_csv("maker_wallets.csv", wallet_rows)
    write_csv("rebate_rows.csv", all_rebate_rows)
    write_csv("rebate_activity.csv", all_activity_rows)

    with open("maker_screen_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    with open("maker_screen_errors.json", "w", encoding="utf-8") as f:
        json.dump(errors, f, indent=2, ensure_ascii=False, default=str)
    with open("maker_screen_details.json", "w", encoding="utf-8") as f:
        json.dump(selected_details, f, indent=2, ensure_ascii=False)

    print("\n=== SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    print("FILES=maker_screen_summary.json,maker_wallets.csv,rebate_rows.csv,rebate_activity.csv,maker_screen_errors.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
