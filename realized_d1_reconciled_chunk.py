#!/usr/bin/env python3
import json, os, math, statistics
from datetime import datetime, timezone

from realized_d1_audit import (
    build_live_cohort, closed_since, gamma_batch, looks_short_crypto,
    positions_all, calendar_rebates, first, fnum
)

UTC=timezone.utc
START_INDEX=int(os.getenv('START_INDEX','1'))
END_INDEX=int(os.getenv('END_INDEX','43'))

def med(xs):
    xs=[x for x in xs if x is not None and math.isfinite(x)]
    return statistics.median(xs) if xs else None

def summarize(rows):
    pnl=[r['realized_short_closed_month'] for r in rows]
    reb=[r['calendar_rebate'] for r in rows]
    net=[a+b for a,b in zip(pnl,reb)]
    return {
        'n':len(rows),
        'median_realized_pnl':med(pnl),'median_rebate':med(reb),'median_net':med(net),
        'sum_realized_pnl':sum(pnl),'sum_rebate':sum(reb),'sum_net':sum(net),
        'negative_realized_count':sum(x<0 for x in pnl),
        'negative_then_positive_with_rebate':sum(a<0 and a+b>0 for a,b in zip(pnl,reb)),
        'still_negative_with_rebate':sum(a+b<0 for a,b in zip(pnl,reb)),
        'median_abs_gap':med([r['abs_leaderboard_gap'] for r in rows]),
        'median_gap_over_lb_vol':med([r['gap_over_leaderboard_vol'] for r in rows]),
        'median_gap_over_short_cost':med([r['gap_over_short_cost_notional'] for r in rows]),
    }

def main():
    now=datetime.now(UTC); month_start=datetime(now.year,now.month,1,tzinfo=UTC)
    cohort=build_live_cohort(now); d1=cohort[:43]
    rows=[]
    from realized_d1_audit import _gamma_cache
    for idx in range(START_INDEX,min(END_INDEX,43)+1):
        r=d1[idx-1]; w=r['wallet']
        closed,cr=closed_since(w,int(month_start.timestamp()))
        if not closed:
            rows.append({'wallet':w,'rank_in_D1':idx,'error':'no closed positions'}); continue
        cids={str(first(x,'conditionId','condition_id') or '').lower() for x in closed if first(x,'conditionId','condition_id')}
        gamma_batch(cids)
        short=[]; total_bought=0.0; short_bought=0.0; short_cost=0.0
        for x in closed:
            cid=str(first(x,'conditionId','condition_id') or '').lower(); m=_gamma_cache.get(cid,{})
            isshort,_=looks_short_crypto(x,m)
            b=fnum(first(x,'totalBought','total_bought')); ap=fnum(first(x,'avgPrice','avg_price'))
            total_bought+=b
            if isshort:
                short.append(x); short_bought+=b; short_cost+=b*ap
        pos,pr=positions_all(w)
        open_value=sum(fnum(first(x,'currentValue','current_value')) for x in pos)
        short_fraction=short_bought/total_bought if total_bought else 0.0
        realized_short=sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in short)
        reb,reb_days=calendar_rebates(w,month_start,now)
        lb_pnl=r.get('overall_pnl'); lb_vol=r.get('overall_vol') or 0.0
        gap=(lb_pnl-realized_short) if lb_pnl is not None else None
        abs_gap=abs(gap) if gap is not None else None
        flat_ratio=open_value/lb_vol if lb_vol else None
        base=(r.get('crypto_share') is not None and r.get('crypto_share')>=.95 and len(short)>=100 and short_fraction>=.90 and flat_ratio is not None and flat_ratio<=.01)
        row={
            'wallet':w,'rank_in_D1':idx,'crypto_share':r.get('crypto_share'),
            'leaderboard_month_pnl':lb_pnl,'leaderboard_month_vol':lb_vol,
            'closed_positions_month':len(closed),'short_closed_positions':len(short),
            'total_bought_shares_closed':total_bought,'short_bought_shares':short_bought,
            'short_cost_notional':short_cost,'short_fraction_by_totalBought':short_fraction,
            'realized_short_closed_month':realized_short,'calendar_rebate':reb,'rebate_payment_events':reb_days,
            'open_positions_count':len(pos),'open_current_value':open_value,'open_value_over_month_vol':flat_ratio,
            'eligible_base':base,'leaderboard_minus_realized_short':gap,'abs_leaderboard_gap':abs_gap,
            'gap_over_leaderboard_vol':abs_gap/lb_vol if abs_gap is not None and lb_vol else None,
            'gap_over_short_cost_notional':abs_gap/short_cost if abs_gap is not None and short_cost else None,
        }
        rows.append(row)
        print('D1REC',idx,w,'base',base,'shortfrac',round(short_fraction,4),'gap',round(abs_gap or 0,2),'gap/lb',row['gap_over_leaderboard_vol'],'gap/cost',row['gap_over_short_cost_notional'],'pnl',round(realized_short,2),'reb',round(reb,2),flush=True)

    base=[r for r in rows if r.get('eligible_base')]
    filters={
        'base':base,
        'gap_lb_2pct':[r for r in base if r.get('gap_over_leaderboard_vol') is not None and r['gap_over_leaderboard_vol']<=.02],
        'short995_gap_lb_1pct':[r for r in base if r['short_fraction_by_totalBought']>=.995 and r.get('gap_over_leaderboard_vol') is not None and r['gap_over_leaderboard_vol']<=.01],
        'gap_cost_2pct':[r for r in base if r.get('gap_over_short_cost_notional') is not None and r['gap_over_short_cost_notional']<=.02],
        'short995_gap_cost_1pct':[r for r in base if r['short_fraction_by_totalBought']>=.995 and r.get('gap_over_short_cost_notional') is not None and r['gap_over_short_cost_notional']<=.01],
        'abs_gap_le_2000':[r for r in base if r.get('abs_leaderboard_gap') is not None and r['abs_leaderboard_gap']<=2000],
    }
    summary={k:summarize(v) for k,v in filters.items()}
    out={'timestamp':now.isoformat(),'start_index':START_INDEX,'end_index':END_INDEX,'summary':summary,'rows':rows}
    path=f'realized_d1_reconciled_{START_INDEX}_{END_INDEX}.json'
    with open(path,'w') as f: json.dump(out,f,indent=2,default=str)
    print('RESULT_FILE='+path); print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__': main()
