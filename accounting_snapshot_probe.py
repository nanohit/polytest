#!/usr/bin/env python3
import csv, io, json, zipfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from closed_loop_audit import get, items, first, fnum, activity_all, closed_positions_since
from datetime import datetime, timezone, timedelta

WALLETS=[
'0x44ab68a9e1272216005366a9a955e5320d28bef3',
'0x04b6d7e930cf9e493c5e6ef24b496294f95594c8',
'0x0feb1bf966bc7f954c2da0293ae2fdc572c5db5d',
]
UA='nanohit-polytest-accounting-snapshot/0.2'

def snapshot(w):
    url='https://data-api.polymarket.com/v1/accounting/snapshot?'+urlencode({'user':w})
    req=Request(url,headers={'User-Agent':UA,'Accept':'application/zip'})
    with urlopen(req,timeout=60) as r:
        raw=r.read(); status=r.status; ctype=r.headers.get('content-type')
    out={'status':status,'content_type':ctype,'bytes':len(raw),'files':{}}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            data=z.read(name).decode('utf-8','replace')
            rows=list(csv.DictReader(io.StringIO(data)))
            numeric={}
            if rows:
                for k in rows[0]:
                    vals=[]
                    for row in rows:
                        try: vals.append(float(row[k]))
                        except: pass
                    if vals:numeric[k]={'sum':sum(vals),'min':min(vals),'max':max(vals),'non_null':len(vals)}
            out['files'][name]={'columns':list(rows[0]) if rows else [],'rows':len(rows),'head':rows[:3],'numeric':numeric}
    return out

def leaderboard(w,period='MONTH'):
    r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':'OVERALL','timePeriod':period,'orderBy':'VOL','user':w,'limit':5,'offset':0})
    rr=items(r.get('data')) if r.get('status')==200 else []
    return rr[0] if rr else None

def positions(w):
    out=[];off=0
    while off<=100000:
        r=get('https://data-api.polymarket.com/positions',{'user':w,'limit':500,'offset':off})
        if r.get('status')!=200:return out,r
        rr=items(r.get('data'));out.extend(rr)
        if len(rr)<500:return out,r
        off+=500
    return out,{'status':200,'warning':'offset_cap'}

now=datetime.now(timezone.utc)
month_start=datetime(now.year,now.month,1,tzinfo=timezone.utc)
st=int(month_start.timestamp()); en=int(now.timestamp())
res=[]
for w in WALLETS:
    s=snapshot(w); lbm=leaderboard(w,'MONTH'); lba=leaderboard(w,'ALL'); pos,_=positions(w)
    evs,_=activity_all(w,st,en)
    cp,_=closed_positions_since(w,st)
    month_rebate=sum(fnum(first(e,'usdcSize','amount','size')) for e in evs)
    closed_realized=sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in cp)
    month_pnl=fnum(first(lbm or {},'pnl'))
    rec={
      'wallet':w,'leaderboard_MONTH':lbm,'leaderboard_ALL':lba,
      'rebate_calendar_month':month_rebate,'closed_positions_calendar_month':len(cp),
      'closed_realized_calendar_month':closed_realized,
      'leaderboard_minus_closed_realized':month_pnl-closed_realized,
      'open_positions_count':len(pos),
      'open_cashPnl_sum':sum(fnum(first(x,'cashPnl','cash_pnl')) for x in pos),
      'open_realizedPnl_sum':sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in pos),
      'open_currentValue_sum':sum(fnum(first(x,'currentValue','current_value')) for x in pos),
      'open_initialValue_sum':sum(fnum(first(x,'initialValue','initial_value')) for x in pos),
      'snapshot':s,
    }
    res.append(rec)
    print('WALLET',w,'month_pnl',month_pnl,'month_rebate',month_rebate,'closed_realized',closed_realized,'gap',rec['leaderboard_minus_closed_realized'],'open_cash',rec['open_cashPnl_sum'],'snapshot_positions_value',s['files'].get('equity.csv',{}).get('numeric',{}).get('positionsValue',{}).get('sum'),flush=True)
with open('accounting_snapshot_probe.json','w') as f:json.dump({'timestamp':now.isoformat(),'month_start':month_start.isoformat(),'wallets':res},f,indent=2,default=str)
print('=== ACCOUNTING SNAPSHOT PROBE V2 ===')
print(json.dumps({'wallets':[{'wallet':r['wallet'],'month_pnl':first(r['leaderboard_MONTH'] or {},'pnl'),'month_vol':first(r['leaderboard_MONTH'] or {},'vol'),'rebate_calendar_month':r['rebate_calendar_month'],'closed_realized_calendar_month':r['closed_realized_calendar_month'],'leaderboard_minus_closed_realized':r['leaderboard_minus_closed_realized'],'open_cashPnl_sum':r['open_cashPnl_sum'],'open_realizedPnl_sum':r['open_realizedPnl_sum'],'snapshot_equity':r['snapshot']['files'].get('equity.csv',{})} for r in res]},indent=2,default=str))
