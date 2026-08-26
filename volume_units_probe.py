#!/usr/bin/env python3
import json, math
from datetime import datetime, timezone, timedelta

from closed_loop_audit import fetch_trade_slice, leaderboard_user, first, fnum

UTC=timezone.utc
WALLETS=[
    '0xf3326d8fc30f7ed01bfade12c706cb52c397b1c0',
    '0x252d7bae5ec87553dcafb23e275c202ac0d3c456',
    '0xe9ed54623043c0e5d6380270f07f0e044c884543',
]

def agg(rows):
    shares=sum(fnum(first(t,'size')) for t in rows)
    notional=sum(fnum(first(t,'size'))*fnum(first(t,'price')) for t in rows)
    complement=sum(fnum(first(t,'size'))*(1-fnum(first(t,'price'))) for t in rows)
    fee_base=sum(fnum(first(t,'size'))*fnum(first(t,'price'))*(1-fnum(first(t,'price'))) for t in rows)
    return {'rows':len(rows),'shares_sum':shares,'notional_sum':notional,'complement_notional_sum':complement,'fee_curve_base_sum':fee_base}

def compare(lb_vol,a):
    out={}
    for k in ('shares_sum','notional_sum','complement_notional_sum'):
        x=a[k]
        out['lb_over_'+k]=lb_vol/x if x else None
        out[k+'_over_lb']=x/lb_vol if lb_vol else None
        out['abs_rel_error_if_'+k]=abs(x-lb_vol)/lb_vol if lb_vol else None
    return out

def main():
    now=datetime.now(UTC)
    month_start=datetime(now.year,now.month,1,tzinfo=UTC)
    out={'timestamp':now.isoformat(),'month_start':month_start.isoformat(),'wallets':[]}
    for w in WALLETS:
        lb_o,_=leaderboard_user(w,'OVERALL','MONTH')
        lb_c,_=leaderboard_user(w,'CRYPTO','MONTH')
        rows=fetch_trade_slice(w,int(month_start.timestamp()),int(now.timestamp()),False)
        a=agg(rows)
        rec={'wallet':w,'leaderboard_overall':lb_o,'leaderboard_crypto':lb_c,'month_trade_aggregate':a}
        if lb_o:
            v=fnum(first(lb_o,'vol')); rec['overall_comparison']=compare(v,a)
        if lb_c:
            v=fnum(first(lb_c,'vol')); rec['crypto_comparison']=compare(v,a)
        out['wallets'].append(rec)
        print('VOLUNIT',w,'rows',len(rows),'shares',round(a['shares_sum'],2),'notional',round(a['notional_sum'],2),'lbO',fnum(first(lb_o or {},'vol')),'lbC',fnum(first(lb_c or {},'vol')),flush=True)

    # DAY semantics/units on the first wallet: test calendar UTC day and rolling 24h.
    w=WALLETS[0]
    lb_day,_=leaderboard_user(w,'OVERALL','DAY')
    windows={
        'utc_today':(datetime(now.year,now.month,now.day,tzinfo=UTC),now),
        'rolling_24h':(now-timedelta(hours=24),now),
        'previous_utc_day':(datetime(now.year,now.month,now.day,tzinfo=UTC)-timedelta(days=1),datetime(now.year,now.month,now.day,tzinfo=UTC)-timedelta(seconds=1)),
    }
    dayprobe={'wallet':w,'leaderboard_day':lb_day,'windows':{}}
    for name,(a0,b0) in windows.items():
        rr=fetch_trade_slice(w,int(a0.timestamp()),int(b0.timestamp()),False)
        aa=agg(rr)
        dayprobe['windows'][name]={'start':a0.isoformat(),'end':b0.isoformat(),**aa}
        if lb_day: dayprobe['windows'][name]['comparison']=compare(fnum(first(lb_day,'vol')),aa)
    out['day_probe']=dayprobe
    with open('volume_units_probe.json','w') as f: json.dump(out,f,indent=2,default=str)
    print('RESULT_FILE=volume_units_probe.json',flush=True)

if __name__=='__main__': main()
