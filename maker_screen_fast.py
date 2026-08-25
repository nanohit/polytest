#!/usr/bin/env python3
import csv, json, math, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA = "nanohit-polytest-maker-screen-fast/0.3"
LOOKBACK_DAYS = 60
TARGET = 50
CANDIDATES_TO_TEST = 100
MIN_REBATE_DAYS = 2
MIN_MAKER_CONDITIONS = 3
MIN_COVERAGE = 0.70


def fnum(x, default=0.0):
    try:
        return default if x in (None, "") else float(x)
    except Exception:
        return default


def first(d, *keys):
    if not isinstance(d, dict): return None
    for k in keys:
        v = d.get(k)
        if v not in (None, ""): return v
    return None


def items(data):
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for k in ("data", "results", "trades", "activity", "positions"):
            if isinstance(data.get(k), list): return data[k]
    return []


def evt_ts(ev):
    x = first(ev, "timestamp", "time", "createdAt", "created_at")
    if x is None: return None
    try:
        if isinstance(x, (int, float)) or (isinstance(x, str) and x.isdigit()):
            n = int(x); return n // 1000 if n > 10_000_000_000 else n
        return int(datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def evt_date(ev):
    t = evt_ts(ev)
    return datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat() if t is not None else None


def get(base, params=None, timeout=30, retries=5):
    url = base + (("?" + urlencode(params, doseq=True)) if params else "")
    last = None
    for a in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                try: data = json.loads(raw)
                except Exception: data = None
                return {"status": r.status, "data": data, "url": url, "prefix": raw[:300]}
        except HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try: data = json.loads(raw)
            except Exception: data = None
            last = {"status": e.code, "data": data, "url": url, "prefix": raw[:300]}
            if e.code == 429:
                ra = data.get("retry_after_seconds") if isinstance(data, dict) else None
                if ra is None: ra = e.headers.get("Retry-After")
                try: wait = max(.5, min(20, float(ra)))
                except Exception: wait = min(20, 1.5 * 2**a)
                time.sleep(wait); continue
            if 500 <= e.code < 600:
                time.sleep(min(10, 2**a)); continue
            return last
        except (URLError, TimeoutError, Exception) as e:
            last = {"status": None, "url": url, "error": repr(e)}
            time.sleep(min(10, 2**a))
    return last


def q(vals, p):
    v = sorted(x for x in vals if x is not None and math.isfinite(x))
    if not v: return None
    if len(v)==1: return v[0]
    z=(len(v)-1)*p; lo=int(math.floor(z)); hi=int(math.ceil(z))
    return v[lo] if lo==hi else v[lo]+(v[hi]-v[lo])*(z-lo)


def chunks(xs, n):
    for i in range(0, len(xs), n): yield xs[i:i+n]


def write_csv(path, rows):
    if not rows:
        open(path,"w").close(); return
    keys=[]; seen=set()
    for r in rows:
        for k in r:
            if k not in seen: seen.add(k); keys.append(k)
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=keys); w.writeheader(); w.writerows(rows)


def main():
    started=datetime.now(timezone.utc); end_ts=int(started.timestamp()); start_ts=int((started-timedelta(days=LOOKBACK_DAYS)).timestamp())
    errors=[]

    # One large recent-trades request: discover active wallets cheaply.
    tr=get("https://data-api.polymarket.com/trades", {"limit":10000,"offset":0,"takerOnly":"false"})
    if tr.get("status") != 200:
        print(json.dumps(tr,indent=2)); return 2
    stat=defaultdict(lambda:{"n":0,"vol":0.0})
    for t in items(tr.get("data")):
        w=first(t,"proxyWallet","proxy_wallet","user")
        if not (isinstance(w,str) and w.startswith("0x")): continue
        stat[w]["n"]+=1; stat[w]["vol"]+=abs(fnum(first(t,"size","shares","amount"))*fnum(first(t,"price"),1.0))
    candidates=sorted(stat, key=lambda w:(stat[w]["n"],stat[w]["vol"]), reverse=True)[:CANDIDATES_TO_TEST]
    print(f"recent trades={len(items(tr.get('data')))} unique={len(stat)} candidates={len(candidates)}",flush=True)

    # One activity call per candidate, 500 max rows, time-bounded to 60 days.
    activity_cache={}; scored=[]
    def fetch_activity(w):
        r=get("https://data-api.polymarket.com/activity", {"user":w,"type":"MAKER_REBATE","limit":500,"offset":0,"start":start_ts,"end":end_ts})
        return w,r
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs=[ex.submit(fetch_activity,w) for w in candidates]
        for i,f in enumerate(as_completed(futs),1):
            w,r=f.result()
            if r.get("status")!=200:
                errors.append({"stage":"activity","wallet":w,"response":r}); continue
            evs=[e for e in items(r.get("data")) if (evt_ts(e) is None or start_ts <= evt_ts(e) <= end_ts)]
            activity_cache[w]=evs
            dates={evt_date(e) for e in evs if evt_date(e)}
            amt=sum(fnum(first(e,"usdcSize","usdc_size","amount","size")) for e in evs)
            if dates: scored.append({"wallet":w,"rebate_days_preview":len(dates),"activity_usdc_preview":amt,"candidate_trades":stat[w]["n"],"candidate_volume_proxy":stat[w]["vol"]})
            if i%20==0: print(f"activity {i}/{len(candidates)} active={len(scored)}",flush=True)
    scored.sort(key=lambda x:(x["activity_usdc_preview"],x["rebate_days_preview"],x["candidate_trades"]),reverse=True)
    selected=scored[:TARGET]
    print(f"selected={len(selected)} rebate-active makers",flush=True)

    # Build all address×active-date rebate lookups, then execute modestly in parallel.
    lookup_tasks=[]
    for s in selected:
        w=s["wallet"]; dates=sorted({evt_date(e) for e in activity_cache.get(w,[]) if evt_date(e)})
        for d in dates: lookup_tasks.append((w,d))
    print(f"rebate lookups={len(lookup_tasks)}",flush=True)

    rebate_rows_by_wallet=defaultdict(list)
    def fetch_rebate(task):
        w,d=task
        return w,d,get("https://clob.polymarket.com/rebates/current", {"date":d,"maker_address":w})
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs=[ex.submit(fetch_rebate,t) for t in lookup_tasks]
        for i,f in enumerate(as_completed(futs),1):
            w,d,r=f.result()
            if r.get("status")!=200:
                errors.append({"stage":"rebate","wallet":w,"date":d,"response":r}); continue
            for x in items(r.get("data")):
                if not isinstance(x,dict): continue
                y=dict(x); y["lookup_date"]=d; y["wallet"]=w; rebate_rows_by_wallet[w].append(y)
            if i%250==0: print(f"rebates {i}/{len(lookup_tasks)}",flush=True)

    # Target only maker condition IDs in closed-positions. The API supports CSV market filtering.
    closed_by_wallet=defaultdict(list)
    def fetch_closed(w, market_chunk, offset=0):
        return w, market_chunk, get("https://data-api.polymarket.com/closed-positions", {"user":w,"market":",".join(market_chunk),"limit":50,"offset":offset,"sortBy":"TIMESTAMP","sortDirection":"DESC"})
    closed_tasks=[]
    for s in selected:
        w=s["wallet"]
        cids=sorted({str(first(r,"condition_id","conditionId")) for r in rebate_rows_by_wallet[w] if first(r,"condition_id","conditionId") and fnum(first(r,"rebated_fees_usdc","rebatedFeesUsdc","earnings"))>0})
        for ch in chunks(cids,20): closed_tasks.append((w,ch))
    print(f"targeted closed-position queries={len(closed_tasks)}",flush=True)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs=[ex.submit(fetch_closed,w,ch) for w,ch in closed_tasks]
        for i,f in enumerate(as_completed(futs),1):
            w,ch,r=f.result()
            if r.get("status")!=200:
                errors.append({"stage":"closed","wallet":w,"markets":ch,"response":r}); continue
            rows=items(r.get("data")); closed_by_wallet[w].extend(rows)
            # 20 conditions can in pathological cases exceed 50 rows. Paginate only if full.
            if len(rows)==50:
                r2=get("https://data-api.polymarket.com/closed-positions", {"user":w,"market":",".join(ch),"limit":50,"offset":50,"sortBy":"TIMESTAMP","sortDirection":"DESC"})
                if r2.get("status")==200: closed_by_wallet[w].extend(items(r2.get("data")))
                else: errors.append({"stage":"closed_page2","wallet":w,"markets":ch,"response":r2})
            if i%50==0: print(f"closed {i}/{len(closed_tasks)}",flush=True)

    wallet_rows=[]; flat_rebates=[]
    for s in selected:
        w=s["wallet"]; evs=activity_cache.get(w,[]); rrs=rebate_rows_by_wallet[w]; cps=closed_by_wallet[w]
        rb=defaultdict(float)
        for r in rrs:
            cid=first(r,"condition_id","conditionId"); amt=fnum(first(r,"rebated_fees_usdc","rebatedFeesUsdc","earnings"))
            if cid and amt>0: rb[str(cid)]+=amt
            flat_rebates.append({"wallet":w,"date":r.get("lookup_date"),"condition_id":cid,"rebated_fees_usdc":amt})
        cp=defaultdict(lambda:{"pnl":0.0,"cost":0.0,"rows":0})
        for p in cps:
            cid=first(p,"conditionId","condition_id")
            if not cid: continue
            cp[str(cid)]["pnl"]+=fnum(first(p,"realizedPnl","realized_pnl"))
            cp[str(cid)]["cost"]+=abs(fnum(first(p,"totalBought","total_bought"))*fnum(first(p,"avgPrice","avg_price")))
            cp[str(cid)]["rows"]+=1
        maker=set(rb); closed=maker & set(cp)
        exact=sum(rb.values()); rb_closed=sum(rb[c] for c in closed); pnl=sum(cp[c]["pnl"] for c in closed); cost=sum(cp[c]["cost"] for c in closed)
        cov=len(closed)/len(maker) if maker else 0.0; rb_cov=rb_closed/exact if exact>0 else 0.0; net=pnl+rb_closed; ret=net/cost if cost>0 else None
        dates={evt_date(e) for e in evs if evt_date(e)}; act=sum(fnum(first(e,"usdcSize","usdc_size","amount","size")) for e in evs)
        wallet_rows.append({
            "wallet":w,"candidate_trades":s["candidate_trades"],"rebate_days":len(dates),"activity_rebate_usdc":act,"exact_rebate_usdc":exact,
            "maker_conditions":len(maker),"closed_maker_conditions":len(closed),"condition_coverage":cov,"rebate_coverage":rb_cov,
            "realized_pnl_on_closed_maker_conditions":pnl,"rebate_on_closed_conditions":rb_closed,"net_pnl_screen":net,"estimated_cost_proxy":cost,"net_return_proxy":ret,
            "evaluable":bool(len(maker)>=MIN_MAKER_CONDITIONS and cov>=MIN_COVERAGE and cost>0 and len(dates)>=MIN_REBATE_DAYS)
        })

    ev=[r for r in wallet_rows if r["evaluable"]]; rets=[r["net_return_proxy"] for r in ev if r["net_return_proxy"] is not None]; nets=[r["net_pnl_screen"] for r in ev]
    summary={
        "started_at":started.isoformat(),"finished_at":datetime.now(timezone.utc).isoformat(),"lookback_days":LOOKBACK_DAYS,
        "selected_makers":len(selected),"evaluable_wallets":len(ev),"errors":len(errors),"rebate_lookup_calls":len(lookup_tasks),"closed_query_calls":len(closed_tasks),
        "median_condition_coverage":q([r["condition_coverage"] for r in wallet_rows],.5),"median_rebate_coverage":q([r["rebate_coverage"] for r in wallet_rows],.5),
        "net_return_proxy":{"p10":q(rets,.1),"p25":q(rets,.25),"median":q(rets,.5),"p75":q(rets,.75),"p90":q(rets,.9)},
        "net_pnl_screen_usdc":{"p10":q(nets,.1),"p25":q(nets,.25),"median":q(nets,.5),"p75":q(nets,.75),"p90":q(nets,.9),"sum":sum(nets)},
        "total_exact_rebates_usdc":sum(r["exact_rebate_usdc"] for r in wallet_rows),
        "warning":"Wallet-level screen: realizedPnl mixes maker/taker/directional activity within maker-identified conditions. Negative result is stronger evidence than positive result."
    }
    med=summary["net_return_proxy"]["median"]; p75=summary["net_return_proxy"]["p75"]
    if len(ev)>=20 and med is not None and p75 is not None and med<0 and p75<=0: verdict="KILL_SIGNAL"
    elif len(ev)>=20 and med is not None and med<0: verdict="NEGATIVE_SIGNAL"
    elif len(ev)<20: verdict="INSUFFICIENT_COVERAGE"
    else: verdict="NO_KILL__DEEPER_ATTRIBUTION_REQUIRED"
    summary["verdict"]=verdict

    wallet_rows.sort(key=lambda r:(r["evaluable"], r["net_return_proxy"] if r["net_return_proxy"] is not None else -999), reverse=True)
    write_csv("maker_wallets.csv",wallet_rows); write_csv("rebate_rows.csv",flat_rebates)
    with open("maker_screen_summary.json","w") as f: json.dump(summary,f,indent=2)
    with open("maker_screen_errors.json","w") as f: json.dump(errors,f,indent=2,default=str)
    print("=== MAKER SCREEN SUMMARY ==="); print(json.dumps(summary,indent=2)); print("FILES=maker_screen_summary.json,maker_wallets.csv,rebate_rows.csv,maker_screen_errors.json")
    return 0

if __name__ == "__main__": sys.exit(main())
