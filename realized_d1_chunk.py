#!/usr/bin/env python3
import json, os
from datetime import datetime, timezone

from realized_d1_audit import (
    build_live_cohort, closed_since, gamma_batch, looks_short_crypto,
    positions_all, calendar_rebates, first, fnum, med
)

UTC = timezone.utc
START_INDEX = int(os.getenv('START_INDEX','1'))
END_INDEX = int(os.getenv('END_INDEX','43'))


def main():
    now=datetime.now(UTC); month_start=datetime(now.year,now.month,1,tzinfo=UTC)
    cohort=build_live_cohort(now); d1=cohort[:43]
    rows=[]
    for idx in range(START_INDEX, min(END_INDEX,43)+1):
        r=d1[idx-1]; w=r['wallet']
        closed,cr=closed_since(w,int(month_start.timestamp()))
        if not closed:
            rows.append({'wallet':w,'rank_in_D1':idx,'error':'no closed positions','crypto_share':r.get('crypto_share')});continue
        cids={str(first(x,'conditionId','condition_id') or '').lower() for x in closed if first(x,'conditionId','condition_id')}
        gamma_batch(cids)
        short=[]; short_bought=0.0; total_bought=0.0
        from realized_d1_audit import _gamma_cache
        for x in closed:
            cid=str(first(x,'conditionId','condition_id') or '').lower(); m=_gamma_cache.get(cid,{})
            isshort,meta=looks_short_crypto(x,m)
            b=fnum(first(x,'totalBought','total_bought')); total_bought+=b
            if isshort:
                short.append(x); short_bought+=b
        pos,pr=positions_all(w)
        open_value=sum(fnum(first(x,'currentValue','current_value')) for x in pos)
        short_fraction=(short_bought/total_bought) if total_bought else 0
        realized_short=sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in short)
        reb,reb_days=calendar_rebates(w,month_start,now)
        month_vol=r.get('overall_vol') or 0
        flat_ratio=open_value/month_vol if month_vol else None
        eligible=(r.get('crypto_share') is not None and r.get('crypto_share')>=.95 and len(short)>=100 and short_fraction>=.90 and flat_ratio is not None and flat_ratio<=.01)
        row={
            'wallet':w,'rank_in_D1':idx,'crypto_share':r.get('crypto_share'),
            'leaderboard_month_pnl':r.get('overall_pnl'),'leaderboard_month_vol':month_vol,
            'closed_positions_month':len(closed),'short_closed_positions':len(short),
            'short_fraction_by_totalBought':short_fraction,
            'realized_short_closed_month':realized_short,'calendar_rebate':reb,
            'rebate_payment_events':reb_days,'open_positions_count':len(pos),
            'open_current_value':open_value,'open_value_over_month_vol':flat_ratio,
            'eligible_realized_only':eligible,
            'leaderboard_minus_realized_short':(r.get('overall_pnl')-realized_short) if r.get('overall_pnl') is not None else None,
        }
        rows.append(row)
        print('D1CHUNK',idx,w,'eligible',eligible,'closed',len(closed),'short',len(short),'realized',round(realized_short,2),'reb',round(reb,2),flush=True)
    elig=[x for x in rows if x.get('eligible_realized_only')]
    pnl=[x['realized_short_closed_month'] for x in elig]; rebates=[x['calendar_rebate'] for x in elig]
    summary={
        'start_index':START_INDEX,'end_index':END_INDEX,'processed_n':len(rows),'eligible_n':len(elig),
        'median_realized_trading_pnl':med(pnl),'sum_realized_trading_pnl':sum(pnl),
        'median_rebate':med(rebates),'sum_rebate':sum(rebates),
        'sum_net_if_rebate_excluded':sum(pnl)+sum(rebates),
        'negative_realized_count':sum(x<0 for x in pnl),
        'negative_then_positive_with_rebate':sum(x['realized_short_closed_month']<0 and x['realized_short_closed_month']+x['calendar_rebate']>0 for x in elig),
        'still_negative_with_rebate':sum(x['realized_short_closed_month']+x['calendar_rebate']<0 for x in elig),
    }
    out={'timestamp':now.isoformat(),'summary':summary,'rows':rows}
    path=f'realized_d1_{START_INDEX}_{END_INDEX}.json'
    with open(path,'w') as f: json.dump(out,f,indent=2,default=str)
    print('RESULT_FILE='+path)
    print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__': main()
