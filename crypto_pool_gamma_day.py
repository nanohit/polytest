#!/usr/bin/env python3
import json, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from closed_loop_audit import get, items, first, fnum, ets

UTC=timezone.utc
DAY=int(os.getenv('DAY','1'))
TRADE_LIMIT=10000
WORKERS=8
TAG_ID='21'  # Gamma /tags/slug/crypto


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def gamma_markets(day0,day1):
    by={}
    # Keyset pagination is required for large market sets; offset pagination fails around offset~900.
    for closed in ('true','false'):
        cursor=None; pages=0
        while pages<500:
            params={
                'tag_id':TAG_ID,'related_tags':'true','closed':closed,
                'start_date_max':iso(day1),'end_date_min':iso(day0),
                'limit':100,'ascending':'true'
            }
            if cursor: params['after_cursor']=cursor
            r=get('https://gamma-api.polymarket.com/markets/keyset',params)
            if r.get('status')!=200:
                raise RuntimeError('gamma keyset markets failed '+str(r))
            data=r.get('data')
            rr=data.get('markets',[]) if isinstance(data,dict) else []
            for m in rr:
                cid=str(first(m,'conditionId','condition_id') or '').lower()
                if cid: by[cid]=m
            cursor=data.get('next_cursor') if isinstance(data,dict) else None
            pages+=1
            if not cursor or not rr: break
        if pages>=500: raise RuntimeError('gamma keyset page cap')
    return list(by.values())


def fetch_market_day(cid,start,end,depth=0):
    r=get('https://data-api.polymarket.com/trades',{
        'market':cid,'takerOnly':'true','limit':TRADE_LIMIT,'offset':0,'start':start,'end':end
    })
    if r.get('status')!=200:
        raise RuntimeError('market trades failed '+str(r))
    rr=items(r.get('data'))
    inwin=[t for t in rr if ets(t) is not None and start<=ets(t)<=end]
    outside=len(rr)-len(inwin)
    if len(rr)<TRADE_LIMIT:
        return inwin, outside, False
    if depth>=22 or end-start<=1:
        raise RuntimeError(f'market slice capped cid={cid} {start}-{end} rows={len(rr)} outside={outside}')
    mid=(start+end)//2
    a,oa,_=fetch_market_day(cid,start,mid,depth+1)
    b,ob,_=fetch_market_day(cid,mid+1,end,depth+1)
    return a+b,oa+ob,True


def fee_meta(m):
    fs=first(m,'feeSchedule','fee_schedule')
    if isinstance(fs,dict):
        rate=fnum(first(fs,'rate'))
        share=fnum(first(fs,'rebateRate','rebate_rate'))
        exp=first(fs,'exponent')
    else:
        rate=share=0.0;exp=None
    return rate,share,exp


def market_one(m,start,end):
    cid=str(first(m,'conditionId','condition_id') or '').lower()
    rows,outside,split=fetch_market_day(cid,start,end)
    rate,share,exp=fee_meta(m)
    clob_fd=None
    if rate<=0:
        r=get('https://clob.polymarket.com/clob-markets/'+cid)
        d=r.get('data') if r.get('status')==200 and isinstance(r.get('data'),dict) else {}
        fd=d.get('fd') if isinstance(d,dict) else None
        if isinstance(fd,dict):
            rate=fnum(first(fd,'r')); exp=first(fd,'e'); clob_fd=fd
    shares=sum(fnum(first(t,'size')) for t in rows)
    notional=sum(fnum(first(t,'size'))*fnum(first(t,'price')) for t in rows)
    base=sum(fnum(first(t,'size'))*fnum(first(t,'price'))*(1-fnum(first(t,'price'))) for t in rows)
    empirical_share=.20 if abs(rate-.07)<=.002 else None
    used_share=share if share>0 else (empirical_share or 0.0)
    return {
        'condition_id':cid,'question':first(m,'question','title'),'slug':first(m,'slug'),
        'rows':len(rows),'outside_rows_discarded':outside,'recursive_split':split,
        'shares':shares,'notional':notional,'fee_curve_base':base,
        'gamma_fee_rate':rate,'gamma_rebate_rate':share,'gamma_exponent':exp,'clob_fd_fallback':clob_fd,
        'crypto_fee_enabled':abs(rate-.07)<=.002,
        'rebate_share_used':used_share,
        'taker_fee_est':rate*base if rate>0 else 0.0,
        'rebate_pool_est':rate*used_share*base if rate>0 and used_share>0 else 0.0,
        'volumeNum':first(m,'volumeNum','volume_num'),'startDate':first(m,'startDate','start_date'),'endDate':first(m,'endDate','end_date'),
    }


def main():
    now=datetime.now(UTC)
    d0=datetime(now.year,now.month,DAY,tzinfo=UTC); d1=d0+timedelta(days=1)
    if d1>datetime(now.year,now.month,now.day,tzinfo=UTC):
        raise SystemExit('DAY must be completed UTC day')
    markets=gamma_markets(d0,d1)
    start=int(d0.timestamp());end=int(d1.timestamp())-1
    rows=[];errs=[]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs={ex.submit(market_one,m,start,end):m for m in markets}
        for i,f in enumerate(as_completed(futs),1):
            m=futs[f]
            try: rows.append(f.result())
            except Exception as e:
                errs.append({'condition_id':str(first(m,'conditionId','condition_id') or '').lower(),'question':first(m,'question','title'),'error':repr(e)})
            if i%100==0: print('MARKETS',i,'/',len(markets),'errors',len(errs),flush=True)
    crypto=[r for r in rows if r['crypto_fee_enabled']]
    active=[r for r in crypto if r['rows']>0]
    pool=sum(r['rebate_pool_est'] for r in crypto)
    fees=sum(r['taker_fee_est'] for r in crypto)
    base=sum(r['fee_curve_base'] for r in crypto)
    shares=sum(r['shares'] for r in crypto); notion=sum(r['notional'] for r in crypto)
    gamma_share_vals=sorted({r['gamma_rebate_rate'] for r in crypto if r['gamma_rebate_rate']>0})
    gamma_rate_vals=sorted({r['gamma_fee_rate'] for r in crypto if r['gamma_fee_rate']>0})
    out={
        'trade_date':d0.date().isoformat(),'gamma_crypto_markets_overlapping_day':len(markets),'markets_fetched_ok':len(rows),'market_errors_n':len(errs),'market_errors':errs,
        'crypto_fee_enabled_markets':len(crypto),'crypto_active_markets_with_taker_trades':len(active),
        'taker_rows':sum(r['rows'] for r in crypto),'taker_shares':shares,'taker_notional':notion,'fee_curve_base':base,
        'taker_fees_est':fees,'rebate_pool_est':pool,'direct_0p014_base_check':0.014*base,
        'fee_rates_seen':gamma_rate_vals,'gamma_rebate_rates_seen':gamma_share_vals,
        'outside_rows_discarded':sum(r['outside_rows_discarded'] for r in rows),
        'top_markets_by_pool':sorted(active,key=lambda r:r['rebate_pool_est'],reverse=True)[:30],
    }
    path=f'crypto_pool_gamma_{d0.date().isoformat()}.json'
    with open(path,'w') as f:json.dump(out,f,indent=2,default=str)
    print('POOLGAMMA',d0.date(),'gamma_markets',len(markets),'crypto_fee',len(crypto),'active',len(active),'rows',out['taker_rows'],'fees',round(fees,2),'pool',round(pool,2),'errors',len(errs),'outside',out['outside_rows_discarded'],'rates',gamma_rate_vals,'shares',gamma_share_vals,flush=True)
    print('RESULT_FILE='+path,flush=True)

if __name__=='__main__':main()
