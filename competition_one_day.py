#!/usr/bin/env python3
import json, os, math, statistics
from datetime import datetime, timezone, timedelta

from deep_empirical_audit import (
    COMPETITION_WALLETS, closed_loop_wallet_day, clob_info,
    market_maker_stats, gamma_market, first, fnum, corr
)

UTC=timezone.utc
DATE_AGO=int(os.getenv('DATE_AGO','2'))
TARGET_N=int(os.getenv('TARGET_N','6'))


def pick_quantiles(rows,n):
    if len(rows)<=n:return rows
    rows=sorted(rows,key=lambda r:r['actual_rebate'])
    out=[];seen=set()
    for i in range(n):
        j=round(i*(len(rows)-1)/(n-1))
        r=rows[j]
        if r['condition_id'] not in seen:
            seen.add(r['condition_id']);out.append(r)
    return out


def main():
    d=(datetime.now(UTC).date()-timedelta(days=DATE_AGO)).isoformat()
    candidates=[]
    for w in COMPETITION_WALLETS:
        try:a=closed_loop_wallet_day(w,d,None)
        except Exception as e:
            print('WALLET_ERR',w,repr(e),flush=True);continue
        for c in a.get('conditions',[]):
            if c.get('reconstructed_fee_curve_base',0)<=0 or c.get('actual_rebate',0)<=0:continue
            info=clob_info(c['condition_id']);fd=info.get('fd') if isinstance(info,dict) else None
            rate=fnum(fd.get('r')) if isinstance(fd,dict) else 0
            if abs(rate-.07)>.002:continue
            candidates.append({
                'wallet':w,'trade_date':d,'condition_id':c['condition_id'],
                'actual_rebate':c['actual_rebate'],'target_base':c['reconstructed_fee_curve_base'],
                'target_ratio':c['actual_over_base'],'title':first(gamma_market(c['condition_id']),'question','title')
            })
    by={}
    for r in candidates:
        cid=r['condition_id']
        if cid not in by or r['actual_rebate']>by[cid]['actual_rebate']:by[cid]=r
    picked=pick_quantiles(list(by.values()),TARGET_N)
    rows=[]
    for i,r in enumerate(picked,1):
        try:s=market_maker_stats(r['condition_id'],d)
        except Exception as e:
            q=dict(r);q['error']=repr(e);rows.append(q);continue
        q=dict(r);q.update(s);rows.append(q)
        print('COMPDATE',d,i,'makers',s['active_filled_makers'],'ratio',q['target_ratio'],'rebate',q['actual_rebate'],flush=True)
    valid=[r for r in rows if r.get('active_filled_makers') is not None and r.get('target_ratio') is not None]
    valid=sorted(valid,key=lambda r:r['active_filled_makers'])
    q=max(1,len(valid)//4) if valid else 0
    summary={
        'trade_date':d,'tested_conditions':len(valid),
        'active_maker_min':min([r['active_filled_makers'] for r in valid],default=None),
        'active_maker_max':max([r['active_filled_makers'] for r in valid],default=None),
        'coefficient_median':statistics.median([r['target_ratio'] for r in valid]) if valid else None,
        'coefficient_min':min([r['target_ratio'] for r in valid],default=None),
        'coefficient_max':max([r['target_ratio'] for r in valid],default=None),
        'corr_log_active_makers_vs_coefficient':corr([math.log(max(1,r['active_filled_makers'])) for r in valid],[r['target_ratio'] for r in valid]) if len(valid)>=3 else None,
        'low_competition_median_coefficient':statistics.median([r['target_ratio'] for r in valid[:q]]) if q else None,
        'high_competition_median_coefficient':statistics.median([r['target_ratio'] for r in valid[-q:]]) if q else None,
    }
    out={'summary':summary,'rows':rows}
    path=f'competition_{d}.json'
    with open(path,'w') as f:json.dump(out,f,indent=2,default=str)
    print('RESULT_FILE='+path)
    print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__':main()
