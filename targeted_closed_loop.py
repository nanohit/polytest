#!/usr/bin/env python3
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from closed_loop_audit import activity_all, ets, first, fnum, closed_loop_wallet_day, pnl_period_probe

WALLETS=[
'0x0feb1bf966bc7f954c2da0293ae2fdc572c5db5d',
'0x04b6d7e930cf9e493c5e6ef24b496294f95594c8',
'0x44ab68a9e1272216005366a9a955e5320d28bef3',
'0x48ea6b561bdef5c384f83b1e05fde6b7af973e30',
'0x6f398b378fd614e2c4c4e3f710d39707ef195030',
'0xf26352798e97aa98264c6ac6e16dfc5bd097129b',
'0x48fee9f552871192f0417637d3f388a7a26efd3f',
'0x84413583462b68f0a379f533bcbdcd089acfbc73',
]

now=datetime.now(timezone.utc)
start=int((now-timedelta(days=14)).timestamp()); end=int(now.timestamp())
audits=[]
for w in WALLETS:
    evs,_=activity_all(w,start,end)
    bypay=defaultdict(float)
    for e in evs:
        t=ets(e)
        if t is None: continue
        pd=datetime.fromtimestamp(t,tz=timezone.utc).date()
        if pd>=now.date(): continue
        bypay[pd]+=fnum(first(e,'usdcSize','usdc_size','amount','size'))
    for payd in sorted(bypay,reverse=True)[:1]:
        trade_d=(payd-timedelta(days=1)).isoformat()
        try:
            a=closed_loop_wallet_day(w,trade_d,bypay[payd])
            audits.append(a)
            print('AUDIT',w,trade_d,'maker',a['maker_rows'],'taker',a['taker_rows'],'rebate',a['actual_rebate_total'],'coverage',a['actual_rebate_covered_by_reconstructed_conditions'],'match',a['condition_match_fraction'],'maker_share',a['maker_share_of_user_notional'],flush=True)
        except Exception as e:
            audits.append({'wallet':w,'trade_date':trade_d,'error':repr(e)})
            print('ERROR',w,trade_d,repr(e),flush=True)

# PnL basis on three representative high-rebate wallets; closed positions are fully paginated by timestamp.
pnl=[pnl_period_probe(w,now) for w in WALLETS[:3]]

valid=[a for a in audits if 'error' not in a]
summary={
    'timestamp':now.isoformat(),
    'audits':audits,
    'pnl_period_probes':pnl,
    'aggregate':{
        'n_valid':len(valid),
        'median_condition_match_fraction': sorted([a['condition_match_fraction'] for a in valid if a.get('condition_match_fraction') is not None])[len([a for a in valid if a.get('condition_match_fraction') is not None])//2] if any(a.get('condition_match_fraction') is not None for a in valid) else None,
        'median_actual_rebate_coverage': sorted([a['actual_rebate_covered_by_reconstructed_conditions'] for a in valid if a.get('actual_rebate_covered_by_reconstructed_conditions') is not None])[len([a for a in valid if a.get('actual_rebate_covered_by_reconstructed_conditions') is not None])//2] if any(a.get('actual_rebate_covered_by_reconstructed_conditions') is not None for a in valid) else None,
    }
}
with open('targeted_closed_loop.json','w') as f: json.dump(summary,f,indent=2,default=str)
print('=== TARGETED CLOSED LOOP ===')
print(json.dumps({'aggregate':summary['aggregate'],'audits':[{k:a.get(k) for k in ('wallet','trade_date','all_trade_rows','taker_rows','maker_rows','actual_rebate_total','actual_rebate_covered_by_reconstructed_conditions','condition_match_fraction','maker_share_of_user_notional','error')} for a in audits],'pnl':pnl},indent=2,default=str))
