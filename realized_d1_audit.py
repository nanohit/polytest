#!/usr/bin/env python3
import json, math, statistics, re
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from closed_loop_audit import get, items, first, fnum, ets, activity_all, build_live_cohort

UTC=timezone.utc
D1_N=43
_gamma_cache={}


def med(xs):
    xs=[x for x in xs if x is not None and math.isfinite(x)]
    return statistics.median(xs) if xs else None


def positions_all(wallet):
    out=[];off=0
    while off<=100000:
        r=get('https://data-api.polymarket.com/positions',{'user':wallet,'limit':500,'offset':off,'sizeThreshold':0})
        if r.get('status')!=200:return out,r
        rr=items(r.get('data'));out.extend(rr)
        if len(rr)<500:return out,r
        off+=500
    return out,{'status':200,'warning':'offset_cap'}


def closed_since(wallet,start_ts):
    out=[];off=0
    while off<=100000:
        r=get('https://data-api.polymarket.com/closed-positions',{'user':wallet,'limit':50,'offset':off,'sortBy':'TIMESTAMP','sortDirection':'DESC'})
        if r.get('status')!=200:return out,r
        rr=items(r.get('data'))
        if not rr:return out,r
        stop=False
        for x in rr:
            t=ets(x)
            if t is not None and t>=start_ts:out.append(x)
            elif t is not None and t<start_ts:stop=True
        if stop or len(rr)<50:return out,r
        off+=50
    return out,{'status':200,'warning':'offset_cap'}


def gamma_batch(cids):
    need=[c.lower() for c in cids if c and c.lower() not in _gamma_cache]
    for i in range(0,len(need),40):
        chunk=need[i:i+40]
        r=get('https://gamma-api.polymarket.com/markets',{'condition_ids':chunk,'limit':len(chunk)})
        rr=items(r.get('data')) if r.get('status')==200 else []
        seen=set()
        for m in rr:
            cid=str(first(m,'conditionId','condition_id') or '').lower()
            if cid:
                _gamma_cache[cid]=m;seen.add(cid)
        for cid in chunk:
            if cid not in seen:_gamma_cache[cid]={}
    return {c:_gamma_cache.get(c,{}) for c in cids}


def parse_dt(x):
    if not x:return None
    try:return datetime.fromisoformat(str(x).replace('Z','+00:00'))
    except:return None


def looks_short_crypto(pos,market):
    title=str(first(pos,'title') or first(market,'question') or '').lower()
    slug=str(first(pos,'slug') or first(market,'slug') or '').lower()
    cat=str(first(market,'category') or '').lower()
    start=parse_dt(first(market,'startDate','start_date')); end=parse_dt(first(market,'endDate','end_date'))
    dur=(end-start).total_seconds() if start and end else None
    text=title+' '+slug
    pattern=('up or down' in text or 'up-or-down' in text or '5-minute' in text or '5 minute' in text or '15-minute' in text or '15 minute' in text)
    shortdur=(dur is not None and 0<dur<=86400)
    crypto=('crypto' in cat or any(k in text for k in ['bitcoin','btc','ethereum','eth ','solana','sol ','xrp']))
    return crypto and (pattern or shortdur), {'gamma_category':cat,'market_duration_sec':dur,'pattern_short':pattern,'short_duration':shortdur}


def calendar_rebates(wallet,month_start,now):
    # Payment day D+1. To cover trading from month_start onward, include payments from month_start+1.
    st=int((month_start+timedelta(days=1)).timestamp()); en=int(now.timestamp())
    evs,_=activity_all(wallet,st,en)
    return sum(fnum(first(e,'usdcSize','amount','size')) for e in evs),len(evs)


def main():
    now=datetime.now(UTC); month_start=datetime(now.year,now.month,1,tzinfo=UTC)
    cohort=build_live_cohort(now); d1=cohort[:D1_N]
    rows=[]
    for idx,r in enumerate(d1,1):
        w=r['wallet']
        closed,cr=closed_since(w,int(month_start.timestamp()))
        if not closed:
            rows.append({'wallet':w,'error':'no closed positions','crypto_share':r.get('crypto_share')});continue
        cids={str(first(x,'conditionId','condition_id') or '').lower() for x in closed if first(x,'conditionId','condition_id')}
        gamma_batch(cids)
        short=[]; long=[]
        short_bought=0.0; total_bought=0.0
        for x in closed:
            cid=str(first(x,'conditionId','condition_id') or '').lower(); m=_gamma_cache.get(cid,{})
            isshort,meta=looks_short_crypto(x,m)
            b=fnum(first(x,'totalBought','total_bought'));total_bought+=b
            rec={'pos':x,'meta':meta}
            if isshort:short.append(rec);short_bought+=b
            else:long.append(rec)
        pos,pr=positions_all(w)
        open_value=sum(fnum(first(x,'currentValue','current_value')) for x in pos)
        open_cash=sum(fnum(first(x,'cashPnl','cash_pnl')) for x in pos)
        short_fraction=(short_bought/total_bought) if total_bought else 0
        realized_short=sum(fnum(first(z['pos'],'realizedPnl','realized_pnl')) for z in short)
        realized_all=sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in closed)
        reb,reb_days=calendar_rebates(w,month_start,now)
        month_vol=r.get('overall_vol') or 0
        flat_ratio=open_value/month_vol if month_vol else None
        eligible=(r.get('crypto_share') is not None and r.get('crypto_share')>=.95 and len(short)>=100 and short_fraction>=.90 and (flat_ratio is not None and flat_ratio<=.01))
        row={
            'wallet':w,'rank_in_D1':idx,'crypto_share':r.get('crypto_share'),'leaderboard_month_pnl':r.get('overall_pnl'),'leaderboard_month_vol':month_vol,
            'closed_positions_month':len(closed),'short_closed_positions':len(short),'short_fraction_by_totalBought':short_fraction,
            'realized_all_closed_month':realized_all,'realized_short_closed_month':realized_short,
            'calendar_rebate':reb,'rebate_payment_events':reb_days,'open_positions_count':len(pos),'open_current_value':open_value,'open_cash_pnl':open_cash,
            'open_value_over_month_vol':flat_ratio,'eligible_realized_only':eligible,
            'leaderboard_minus_realized_short':(r.get('overall_pnl')-realized_short) if r.get('overall_pnl') is not None else None,
        }
        rows.append(row)
        print('D1R',idx,w,'eligible',eligible,'shortfrac',round(short_fraction,3),'closed',len(closed),'short',len(short),'realized',round(realized_short,2),'reb',round(reb,2),'open/vol',flat_ratio,flush=True)
    elig=[x for x in rows if x.get('eligible_realized_only')]
    pnl=[x['realized_short_closed_month'] for x in elig]; rebates=[x['calendar_rebate'] for x in elig]
    summary={
        'D1_n':len(d1),'eligible_n':len(elig),
        'median_realized_trading_pnl':med(pnl),'sum_realized_trading_pnl':sum(pnl),
        'median_rebate':med(rebates),'sum_rebate':sum(rebates),
        'sum_net_if_rebate_excluded_from_trading_pnl':sum(pnl)+sum(rebates),
        'negative_realized_count':sum(x<0 for x in pnl),
        'negative_then_positive_with_rebate':sum(x['realized_short_closed_month']<0 and x['realized_short_closed_month']+x['calendar_rebate']>0 for x in elig),
        'still_negative_with_rebate':sum(x['realized_short_closed_month']+x['calendar_rebate']<0 for x in elig),
        'median_loss_over_rebate_among_losers':med([-x['realized_short_closed_month']/x['calendar_rebate'] for x in elig if x['realized_short_closed_month']<0 and x['calendar_rebate']>0]),
        'median_abs_leaderboard_gap':med([abs(x['leaderboard_minus_realized_short']) for x in elig]),
    }
    out={'timestamp':now.isoformat(),'month_start':month_start.isoformat(),'summary':summary,'rows':rows}
    with open('realized_d1_audit.json','w') as f:json.dump(out,f,indent=2,default=str)
    print('=== REALIZED D1 SUMMARY ===')
    print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__':main()
