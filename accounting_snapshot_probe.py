#!/usr/bin/env python3
import csv, io, json, zipfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from closed_loop_audit import get, items, first, fnum, activity_all
from datetime import datetime, timezone, timedelta

WALLETS=[
'0x44ab68a9e1272216005366a9a955e5320d28bef3',
'0x04b6d7e930cf9e493c5e6ef24b496294f95594c8',
'0x0feb1bf966bc7f954c2da0293ae2fdc572c5db5d',
]
UA='nanohit-polytest-accounting-snapshot/0.1'

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
                    if vals:
                        numeric[k]={'sum':sum(vals),'min':min(vals),'max':max(vals),'non_null':len(vals)}
            out['files'][name]={'columns':list(rows[0]) if rows else [],'rows':len(rows),'head':rows[:3],'numeric':numeric}
    return out

def leaderboard(w, period='MONTH'):
    r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':'OVERALL','timePeriod':period,'orderBy':'VOL','user':w,'limit':5,'offset':0})
    rr=items(r.get('data')) if r.get('status')==200 else []
    return rr[0] if rr else None

def positions(w):
    out=[]; off=0
    while off<=100000:
        r=get('https://data-api.polymarket.com/positions',{'user':w,'limit':500,'offset':off})
        if r.get('status')!=200: return out,r
        rr=items(r.get('data')); out.extend(rr)
        if len(rr)<500:return out,r
        off+=500
    return out,{'status':200,'warning':'offset_cap'}

now=datetime.now(timezone.utc); st=int((now-timedelta(days=31)).timestamp()); en=int(now.timestamp())
res=[]
for w in WALLETS:
    s=snapshot(w)
    lbm=leaderboard(w,'MONTH'); lba=leaderboard(w,'ALL')
    pos,pr=positions(w)
    evs,_=activity_all(w,st,en)
    rec={
      'wallet':w,'leaderboard_MONTH':lbm,'leaderboard_ALL':lba,
      'rebate_31d':sum(fnum(first(e,'usdcSize','amount','size')) for e in evs),
      'open_positions_count':len(pos),
      'open_cashPnl_sum':sum(fnum(first(x,'cashPnl','cash_pnl')) for x in pos),
      'open_realizedPnl_sum':sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in pos),
      'open_currentValue_sum':sum(fnum(first(x,'currentValue','current_value')) for x in pos),
      'open_initialValue_sum':sum(fnum(first(x,'initialValue','initial_value')) for x in pos),
      'snapshot':s,
    }
    res.append(rec)
    print('WALLET',w,'month_pnl',first(lbm or {},'pnl'),'rebate31',rec['rebate_31d'],'open_cash',rec['open_cashPnl_sum'],'snapshot_files',[(k,v['rows'],v['columns']) for k,v in s['files'].items()],flush=True)
with open('accounting_snapshot_probe.json','w') as f:json.dump({'timestamp':now.isoformat(),'wallets':res},f,indent=2,default=str)
print('=== ACCOUNTING SNAPSHOT PROBE ===')
print(json.dumps({'wallets':[{'wallet':r['wallet'],'month_pnl':first(r['leaderboard_MONTH'] or {},'pnl'),'month_vol':first(r['leaderboard_MONTH'] or {},'vol'),'rebate_31d':r['rebate_31d'],'open_cashPnl_sum':r['open_cashPnl_sum'],'open_realizedPnl_sum':r['open_realizedPnl_sum'],'snapshot':{k:{'rows':v['rows'],'columns':v['columns'],'numeric':v['numeric']} for k,v in r['snapshot']['files'].items()}} for r in res]},indent=2,default=str))
