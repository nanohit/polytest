#!/usr/bin/env python3
import csv, json, math, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA='nanohit-polytest-leaderboard-screen/0.1'
TARGET=50
LOOKBACK_DAYS=31
MAX_LEADERBOARD=500
WORKERS=8


def fnum(x, default=0.0):
    try: return default if x in (None,'') else float(x)
    except Exception: return default

def first(d,*keys):
    if not isinstance(d,dict): return None
    for k in keys:
        v=d.get(k)
        if v not in (None,''): return v
    return None

def items(x):
    if isinstance(x,list): return x
    if isinstance(x,dict):
        for k in ('data','results','activity'):
            if isinstance(x.get(k),list): return x[k]
    return []

def ets(ev):
    x=first(ev,'timestamp','time','createdAt','created_at')
    if x is None: return None
    try:
        if isinstance(x,(int,float)) or (isinstance(x,str) and x.isdigit()):
            n=int(x); return n//1000 if n>10_000_000_000 else n
        return int(datetime.fromisoformat(str(x).replace('Z','+00:00')).timestamp())
    except Exception: return None

def edate(ev):
    t=ets(ev)
    return datetime.fromtimestamp(t,tz=timezone.utc).date().isoformat() if t is not None else None

def get(base,params=None,retries=4,timeout=25):
    url=base+(('?'+urlencode(params,doseq=True)) if params else '')
    last=None
    for a in range(retries):
        try:
            req=Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
            with urlopen(req,timeout=timeout) as r:
                raw=r.read().decode('utf-8','replace')
                try:data=json.loads(raw)
                except Exception:data=None
                return {'status':r.status,'data':data,'url':url,'prefix':raw[:300]}
        except HTTPError as e:
            raw=e.read().decode('utf-8','replace')
            try:data=json.loads(raw)
            except Exception:data=None
            last={'status':e.code,'data':data,'url':url,'prefix':raw[:300]}
            if e.code==429:
                ra=(data.get('retry_after_seconds') if isinstance(data,dict) else None) or e.headers.get('Retry-After')
                try:w=max(.5,min(15,float(ra)))
                except Exception:w=min(15,1.5*2**a)
                time.sleep(w); continue
            if 500<=e.code<600:
                time.sleep(min(8,2**a)); continue
            return last
        except (URLError,TimeoutError,Exception) as e:
            last={'status':None,'url':url,'error':repr(e)}; time.sleep(min(8,2**a))
    return last

def q(v,p):
    a=sorted(x for x in v if x is not None and math.isfinite(x))
    if not a:return None
    if len(a)==1:return a[0]
    z=(len(a)-1)*p; lo=int(math.floor(z)); hi=int(math.ceil(z))
    return a[lo] if lo==hi else a[lo]+(a[hi]-a[lo])*(z-lo)

def write_csv(path,rows):
    if not rows: open(path,'w').close(); return
    keys=[]; seen=set()
    for r in rows:
        for k in r:
            if k not in seen: seen.add(k); keys.append(k)
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=keys); w.writeheader(); w.writerows(rows)

def main():
    now=datetime.now(timezone.utc); end=int(now.timestamp()); start=int((now-timedelta(days=LOOKBACK_DAYS)).timestamp())
    errors=[]; leaderboard=[]
    # Pull high-volume crypto traders in pages; max 500 wallets.
    for off in range(0,MAX_LEADERBOARD,50):
        r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':'CRYPTO','timePeriod':'MONTH','orderBy':'VOL','limit':50,'offset':off})
        if r.get('status')!=200:
            errors.append({'stage':'leaderboard','offset':off,'response':r}); break
        rows=items(r.get('data'))
        leaderboard.extend(rows)
        print(f'leaderboard offset={off} rows={len(rows)} total={len(leaderboard)}',flush=True)
        if len(rows)<50: break

    # Deduplicate wallets and preserve leaderboard PnL/vol.
    by_wallet={}
    for x in leaderboard:
        w=first(x,'proxyWallet','proxy_wallet')
        if isinstance(w,str) and w.startswith('0x') and w not in by_wallet:
            by_wallet[w]=x

    def activity(w):
        r=get('https://data-api.polymarket.com/activity',{'user':w,'type':'MAKER_REBATE','limit':500,'offset':0,'start':start,'end':end})
        return w,r

    maker_rows=[]
    wallets=list(by_wallet)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(activity,w) for w in wallets]
        for i,f in enumerate(as_completed(futs),1):
            w,r=f.result()
            if r.get('status')!=200:
                errors.append({'stage':'activity','wallet':w,'response':r}); continue
            evs=[]
            for e in items(r.get('data')):
                t=ets(e)
                if t is None or start<=t<=end: evs.append(e)
            if not evs: continue
            dates=sorted({edate(e) for e in evs if edate(e)})
            rebate=sum(fnum(first(e,'usdcSize','usdc_size','amount','size')) for e in evs)
            lb=by_wallet[w]
            pnl=fnum(first(lb,'pnl'))
            vol=fnum(first(lb,'vol'))
            maker_rows.append({
                'wallet':w,'rank':first(lb,'rank'),'userName':first(lb,'userName','username'),
                'leaderboard_pnl_month':pnl,'leaderboard_vol_month':vol,
                'rebate_days_31d':len(dates),'maker_rebate_activity_usdc_31d':rebate,
                'pnl_plus_rebate_screen':pnl+rebate,
                'pnl_over_volume':pnl/vol if vol>0 else None,
                'pnl_plus_rebate_over_volume':(pnl+rebate)/vol if vol>0 else None,
            })
            if i%50==0: print(f'activity checked={i}/{len(wallets)} rebate_makers={len(maker_rows)}',flush=True)

    maker_rows.sort(key=lambda r:(r['leaderboard_vol_month']),reverse=True)
    selected=maker_rows[:TARGET]
    # Spot-check a few active maker dates via /rebates/current, not for exhaustive attribution.
    spot=[]
    for row in selected[:5]:
        w=row['wallet']
        ar=get('https://data-api.polymarket.com/activity',{'user':w,'type':'MAKER_REBATE','limit':50,'offset':0,'start':start,'end':end})
        dates=sorted({edate(e) for e in items(ar.get('data')) if edate(e)},reverse=True)
        if not dates: continue
        d=dates[0]
        rr=get('https://clob.polymarket.com/rebates/current',{'date':d,'maker_address':w})
        exact=sum(fnum(first(x,'rebated_fees_usdc','rebatedFeesUsdc','earnings')) for x in items(rr.get('data'))) if rr.get('status')==200 else None
        spot.append({'wallet':w,'date':d,'rebates_current_status':rr.get('status'),'exact_condition_sum':exact,'rows':len(items(rr.get('data'))) if rr.get('status')==200 else None})

    nets=[r['pnl_plus_rebate_screen'] for r in selected]; pnls=[r['leaderboard_pnl_month'] for r in selected]; norm=[r['pnl_plus_rebate_over_volume'] for r in selected]
    summary={
        'started_at':now.isoformat(),'finished_at':datetime.now(timezone.utc).isoformat(),
        'leaderboard_wallets_checked':len(by_wallet),'rebate_active_found':len(maker_rows),'selected_makers':len(selected),
        'errors':len(errors),'lookback_activity_days':LOOKBACK_DAYS,
        'leaderboard_period':'MONTH','leaderboard_category':'CRYPTO','selection_order':'VOL',
        'pnl_month_usdc':{'p10':q(pnls,.1),'p25':q(pnls,.25),'median':q(pnls,.5),'p75':q(pnls,.75),'p90':q(pnls,.9),'sum':sum(pnls)},
        'pnl_plus_rebate_screen_usdc':{'p10':q(nets,.1),'p25':q(nets,.25),'median':q(nets,.5),'p75':q(nets,.75),'p90':q(nets,.9),'sum':sum(nets)},
        'pnl_plus_rebate_over_volume':{'p10':q(norm,.1),'p25':q(norm,.25),'median':q(norm,.5),'p75':q(norm,.75),'p90':q(norm,.9)},
        'total_rebate_activity_usdc':sum(r['maker_rebate_activity_usdc_31d'] for r in selected),
        'median_rebate_days':q([r['rebate_days_31d'] for r in selected],.5),
        'spot_checks':spot,
        'caveats':[
            'Leaderboard pnl semantics are wallet/category/period level, not maker-fill attribution.',
            'Maker rebate activity may or may not already be reflected in leaderboard pnl; report both pnl and pnl+rebate as a conservative diagnostic, not accounting truth.',
            'Positive result cannot prove maker profitability; a deeply negative result even after adding rebate is a strong kill signal for generic rebate-maker economics.'
        ]
    }
    med=summary['pnl_plus_rebate_screen_usdc']['median']; p75=summary['pnl_plus_rebate_screen_usdc']['p75']
    if len(selected)>=30 and med is not None and p75 is not None and med<0 and p75<=0: verdict='STRONG_KILL_SIGNAL'
    elif len(selected)>=30 and med is not None and med<0: verdict='NEGATIVE_SIGNAL'
    elif len(selected)<30: verdict='INSUFFICIENT_REBATE_MAKERS_IN_TOP_VOLUME_COHORT'
    else: verdict='NO_KILL__NEEDS_MAKER_FILL_ATTRIBUTION'
    summary['verdict']=verdict
    write_csv('leaderboard_maker_wallets.csv',selected)
    with open('leaderboard_maker_summary.json','w') as f: json.dump(summary,f,indent=2)
    with open('leaderboard_maker_errors.json','w') as f: json.dump(errors,f,indent=2,default=str)
    print('=== LEADERBOARD MAKER SCREEN ==='); print(json.dumps(summary,indent=2)); print('FILES=leaderboard_maker_summary.json,leaderboard_maker_wallets.csv,leaderboard_maker_errors.json')
    return 0
if __name__=='__main__': sys.exit(main())
