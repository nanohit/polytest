#!/usr/bin/env python3
import csv, json, math, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA='nanohit-polytest-leaderboard-screen/0.2'
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

def q(vals,p):
    a=sorted(x for x in vals if x is not None and math.isfinite(x))
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

def stats(rows):
    pnl=[r['leaderboard_pnl_month'] for r in rows]
    upper=[r['pnl_plus_rebate_upper_bound'] for r in rows]
    norm=[r['pnl_over_volume'] for r in rows if r['pnl_over_volume'] is not None]
    norm_upper=[r['pnl_plus_rebate_over_volume_upper_bound'] for r in rows if r['pnl_plus_rebate_over_volume_upper_bound'] is not None]
    return {
        'n':len(rows),
        'pnl_usdc':{'p10':q(pnl,.1),'p25':q(pnl,.25),'median':q(pnl,.5),'p75':q(pnl,.75),'p90':q(pnl,.9),'sum':sum(pnl)},
        'pnl_over_volume':{'p10':q(norm,.1),'p25':q(norm,.25),'median':q(norm,.5),'p75':q(norm,.75),'p90':q(norm,.9)},
        'negative_pnl_fraction':sum(1 for x in pnl if x<0)/len(pnl) if pnl else None,
        'rebate_usdc_sum':sum(r['maker_rebate_activity_usdc_31d'] for r in rows),
        'pnl_plus_rebate_upper_bound_usdc':{'p10':q(upper,.1),'p25':q(upper,.25),'median':q(upper,.5),'p75':q(upper,.75),'p90':q(upper,.9),'sum':sum(upper)},
        'pnl_plus_rebate_over_volume_upper_bound':{'p10':q(norm_upper,.1),'p25':q(norm_upper,.25),'median':q(norm_upper,.5),'p75':q(norm_upper,.75),'p90':q(norm_upper,.9)},
        'negative_upper_bound_fraction':sum(1 for x in upper if x<0)/len(upper) if upper else None,
        'median_rebate_days':q([r['rebate_days_31d'] for r in rows],.5),
        'median_rebate_over_volume':q([r['rebate_over_volume'] for r in rows if r['rebate_over_volume'] is not None],.5),
    }

def main():
    now=datetime.now(timezone.utc); today=now.date().isoformat(); end=int(now.timestamp()); start=int((now-timedelta(days=LOOKBACK_DAYS)).timestamp())
    errors=[]; leaderboard=[]
    for off in range(0,MAX_LEADERBOARD,50):
        r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':'CRYPTO','timePeriod':'MONTH','orderBy':'VOL','limit':50,'offset':off})
        if r.get('status')!=200:
            errors.append({'stage':'leaderboard','offset':off,'response':r}); break
        rows=items(r.get('data')); leaderboard.extend(rows)
        print(f'leaderboard offset={off} rows={len(rows)} total={len(leaderboard)}',flush=True)
        if len(rows)<50: break

    by_wallet={}
    for x in leaderboard:
        w=first(x,'proxyWallet','proxy_wallet')
        if isinstance(w,str) and w.startswith('0x') and w not in by_wallet: by_wallet[w]=x

    activity_cache={}
    def activity(w):
        return w,get('https://data-api.polymarket.com/activity',{'user':w,'type':'MAKER_REBATE','limit':500,'offset':0,'start':start,'end':end})

    maker_rows=[]; wallets=list(by_wallet)
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
            activity_cache[w]=evs
            if not evs: continue
            dates=sorted({edate(e) for e in evs if edate(e)})
            rebate=sum(fnum(first(e,'usdcSize','usdc_size','amount','size')) for e in evs)
            lb=by_wallet[w]; pnl=fnum(first(lb,'pnl')); vol=fnum(first(lb,'vol'))
            try: rank_num=int(first(lb,'rank'))
            except Exception: rank_num=None
            maker_rows.append({
                'wallet':w,'rank':first(lb,'rank'),'rank_num':rank_num,'userName':first(lb,'userName','username'),
                'leaderboard_pnl_month':pnl,'leaderboard_vol_month':vol,'rebate_days_31d':len(dates),
                'maker_rebate_activity_usdc_31d':rebate,'rebate_over_volume':rebate/vol if vol>0 else None,
                'pnl_over_volume':pnl/vol if vol>0 else None,
                'pnl_plus_rebate_upper_bound':pnl+rebate,
                'pnl_plus_rebate_over_volume_upper_bound':(pnl+rebate)/vol if vol>0 else None,
            })
            if i%50==0: print(f'activity checked={i}/{len(wallets)} rebate_makers={len(maker_rows)}',flush=True)

    maker_rows.sort(key=lambda r:r['rank_num'] if r['rank_num'] is not None else 10**9)
    buckets={
        'rank_1_50':[r for r in maker_rows if r['rank_num'] is not None and 1<=r['rank_num']<=50],
        'rank_51_100':[r for r in maker_rows if r['rank_num'] is not None and 51<=r['rank_num']<=100],
        'rank_101_200':[r for r in maker_rows if r['rank_num'] is not None and 101<=r['rank_num']<=200],
        'rank_201_500':[r for r in maker_rows if r['rank_num'] is not None and 201<=r['rank_num']<=500],
    }

    # Validate /activity rebate sums against /rebates/current on the latest COMPLETED activity day.
    spot=[]
    for row in maker_rows[:10]:
        w=row['wallet']; evs=activity_cache.get(w,[])
        finalized=sorted({edate(e) for e in evs if edate(e) and edate(e)<today},reverse=True)
        if not finalized: continue
        d=finalized[0]
        activity_day=sum(fnum(first(e,'usdcSize','usdc_size','amount','size')) for e in evs if edate(e)==d)
        same=get('https://clob.polymarket.com/rebates/current',{'date':d,'maker_address':w})
        prev=(datetime.fromisoformat(d).date()-timedelta(days=1)).isoformat()
        prior=get('https://clob.polymarket.com/rebates/current',{'date':prev,'maker_address':w})
        same_sum=sum(fnum(first(x,'rebated_fees_usdc','rebatedFeesUsdc','earnings')) for x in items(same.get('data'))) if same.get('status')==200 else None
        prev_sum=sum(fnum(first(x,'rebated_fees_usdc','rebatedFeesUsdc','earnings')) for x in items(prior.get('data'))) if prior.get('status')==200 else None
        spot.append({'wallet':w,'activity_date':d,'activity_day_usdc':activity_day,'same_date_status':same.get('status'),'same_date_exact_sum':same_sum,'same_date_rows':len(items(same.get('data'))) if same.get('status')==200 else None,'prior_date':prev,'prior_date_status':prior.get('status'),'prior_date_exact_sum':prev_sum,'prior_date_rows':len(items(prior.get('data'))) if prior.get('status')==200 else None})

    summary={
        'started_at':now.isoformat(),'finished_at':datetime.now(timezone.utc).isoformat(),
        'leaderboard_wallets_checked':len(by_wallet),'rebate_active_found':len(maker_rows),'errors':len(errors),
        'lookback_activity_days':LOOKBACK_DAYS,'leaderboard_period':'MONTH','leaderboard_category':'CRYPTO','leaderboard_order':'VOL',
        'all_rebate_active_top500':stats(maker_rows),
        'rank_buckets':{k:stats(v) for k,v in buckets.items()},
        'spot_checks_finalized_dates':spot,
        'caveats':[
            'Leaderboard pnl is wallet/category/period-level and is not maker-fill attribution.',
            'Official docs do not specify whether leaderboard pnl includes maker rebates; pnl+rebate is therefore labeled an upper-bound diagnostic until reconciled.',
            'The universe is top-500 CRYPTO wallets by monthly volume, not all makers; this measures economically active participants.',
            'A positive result does not prove maker fills are profitable. A negative result would have been a much stronger kill signal.'
        ]
    }
    med=summary['all_rebate_active_top500']['pnl_usdc']['median']; p75=summary['all_rebate_active_top500']['pnl_usdc']['p75']
    if len(maker_rows)>=100 and med is not None and p75 is not None and med<0 and p75<=0: verdict='STRONG_KILL_SIGNAL'
    elif len(maker_rows)>=100 and med is not None and med<0: verdict='NEGATIVE_SIGNAL'
    elif len(maker_rows)<100: verdict='INSUFFICIENT_REBATE_MAKER_COHORT'
    else: verdict='NO_KILL__MAKER_FILL_ATTRIBUTION_REQUIRED'
    summary['verdict']=verdict
    write_csv('leaderboard_maker_wallets.csv',maker_rows)
    with open('leaderboard_maker_summary.json','w') as f: json.dump(summary,f,indent=2)
    with open('leaderboard_maker_errors.json','w') as f: json.dump(errors,f,indent=2,default=str)
    print('=== LEADERBOARD MAKER SCREEN V2 ==='); print(json.dumps(summary,indent=2)); print('FILES=leaderboard_maker_summary.json,leaderboard_maker_wallets.csv,leaderboard_maker_errors.json')
    return 0
if __name__=='__main__': sys.exit(main())
