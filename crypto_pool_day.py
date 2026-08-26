#!/usr/bin/env python3
import json, os, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from closed_loop_audit import get, items, first, fnum

UTC=timezone.utc
TRADE_LIMIT=10000
DAY=int(os.getenv('DAY','1'))
WORKERS=8

def fetch_global(start,end,depth=0):
    r=get('https://data-api.polymarket.com/trades',{
        'takerOnly':'true','limit':TRADE_LIMIT,'offset':0,'start':start,'end':end
    })
    if r.get('status')!=200:
        raise RuntimeError('global taker fetch failed '+str(r))
    rr=items(r.get('data'))
    if len(rr)<TRADE_LIMIT:
        return rr
    if depth>=24 or end-start<=1:
        raise RuntimeError(f'global slice capped {start}-{end} rows={len(rr)}')
    mid=(start+end)//2
    return fetch_global(start,mid,depth+1)+fetch_global(mid+1,end,depth+1)

def clob_meta(cid):
    r=get('https://clob.polymarket.com/clob-markets/'+cid)
    d=r.get('data') if r.get('status')==200 and isinstance(r.get('data'),dict) else {}
    fd=d.get('fd') if isinstance(d,dict) else None
    rate=fnum(fd.get('r')) if isinstance(fd,dict) else None
    return cid,{'status':r.get('status'),'fd_rate':rate,'fd':fd,'question':first(d,'question','title')}

def main():
    now=datetime.now(UTC)
    dt=datetime(now.year,now.month,DAY,tzinfo=UTC)
    if dt.date()>=now.date():
        raise SystemExit('DAY must be completed UTC day')
    start=int(dt.timestamp()); end=start+86399
    rows=fetch_global(start,end)
    cids=sorted({str(first(t,'conditionId','condition_id') or '').lower() for t in rows if first(t,'conditionId','condition_id')})
    meta={}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(clob_meta,c) for c in cids]
        for i,f in enumerate(as_completed(futs),1):
            c,m=f.result(); meta[c]=m
            if i%100==0: print('META',i,'/',len(cids),flush=True)

    byc={}
    total_base=total_notional=total_shares=0.0
    crypto_base=crypto_notional=crypto_shares=0.0
    unclassified_base=0.0
    for t in rows:
        cid=str(first(t,'conditionId','condition_id') or '').lower()
        C=fnum(first(t,'size')); p=fnum(first(t,'price'))
        base=C*p*(1-p); notional=C*p
        total_base+=base; total_notional+=notional; total_shares+=C
        m=meta.get(cid,{}); rate=m.get('fd_rate')
        b=byc.setdefault(cid,{'condition_id':cid,'fd_rate':rate,'shares':0.0,'notional':0.0,'fee_curve_base':0.0,'rows':0})
        b['shares']+=C; b['notional']+=notional; b['fee_curve_base']+=base; b['rows']+=1
        if rate is None:
            unclassified_base+=base
        if rate is not None and abs(rate-0.07)<=0.002:
            crypto_base+=base; crypto_notional+=notional; crypto_shares+=C

    conds=list(byc.values())
    crypto_conds=[x for x in conds if x.get('fd_rate') is not None and abs(x['fd_rate']-0.07)<=0.002]
    crypto_conds.sort(key=lambda x:x['fee_curve_base'],reverse=True)
    taker_fee_est=0.07*crypto_base
    rebate_pool_est=0.20*taker_fee_est
    out={
        'trade_date':dt.date().isoformat(),'taker_rows':len(rows),'unique_conditions':len(cids),
        'crypto_conditions':len(crypto_conds),'total_shares':total_shares,'total_notional':total_notional,'total_fee_curve_base':total_base,
        'crypto_shares':crypto_shares,'crypto_notional':crypto_notional,'crypto_fee_curve_base':crypto_base,
        'crypto_taker_fees_est_0p07':taker_fee_est,'crypto_rebate_pool_est_20pct':rebate_pool_est,
        'crypto_rebate_pool_est_direct_0p014':0.014*crypto_base,
        'unclassified_fee_curve_base':unclassified_base,
        'unclassified_base_fraction':unclassified_base/total_base if total_base else None,
        'top_crypto_conditions':crypto_conds[:25],
    }
    path=f'crypto_pool_{dt.date().isoformat()}.json'
    with open(path,'w') as f: json.dump(out,f,indent=2,default=str)
    print('POOL',dt.date(),'rows',len(rows),'conds',len(cids),'crypto',len(crypto_conds),'base',crypto_base,'fees',taker_fee_est,'rebate',rebate_pool_est,'unclassified_frac',out['unclassified_base_fraction'],flush=True)
    print('RESULT_FILE='+path,flush=True)

if __name__=='__main__': main()
