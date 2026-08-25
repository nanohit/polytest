#!/usr/bin/env python3
import csv, json, math, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA='nanohit-polytest-maker-intensity/0.1'
MAX_CRYPTO_LEADERBOARD=500
ACTIVITY_PAGE=100
WORKERS=10
LOOKBACK_DAYS=31
KNOWN_WALLET='0xc69bd5567b40ef4d11922eaa57e1f9be1c642076'


def fnum(x, default=0.0):
    try: return default if x in (None,'') else float(x)
    except Exception: return default

def first(d,*keys):
    if not isinstance(d,dict): return None
    for k in keys:
        v=d.get(k)
        if v not in (None,''): return v
    return None

def items(x):
    if isinstance(x,list): return x
    if isinstance(x,dict):
        for k in ('data','results','activity','positions','trades'):
            if isinstance(x.get(k),list): return x[k]
    return []

def ets(ev):
    x=first(ev,'timestamp','time','createdAt','created_at')
    if x is None: return None
    try:
        if isinstance(x,(int,float)) or (isinstance(x,str) and x.isdigit()):
            n=int(x); return n//1000 if n>10_000_000_000 else n
        return int(datetime.fromisoformat(str(x).replace('Z','+00:00')).timestamp())
    except Exception: return None

def edate(ev):
    t=ets(ev)
    return datetime.fromtimestamp(t,tz=timezone.utc).date().isoformat() if t is not None else None

def get(base,params=None,retries=5,timeout=30):
    url=base+(('?'+urlencode(params,doseq=True)) if params else '')
    last=None
    for a in range(retries):
        try:
            req=Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
            with urlopen(req,timeout=timeout) as r:
                raw=r.read().decode('utf-8','replace')
                try:data=json.loads(raw)
                except Exception:data=None
                return {'status':r.status,'data':data,'raw_type':type(data).__name__,'url':url,'prefix':raw[:300]}
        except HTTPError as e:
            raw=e.read().decode('utf-8','replace')
            try:data=json.loads(raw)
            except Exception:data=None
            last={'status':e.code,'data':data,'raw_type':type(data).__name__,'url':url,'prefix':raw[:300]}
            if e.code==429:
                ra=(data.get('retry_after_seconds') if isinstance(data,dict) else None) or e.headers.get('Retry-After')
                try:w=max(.5,min(20,float(ra)))
                except Exception:w=min(20,1.5*2**a)
                time.sleep(w); continue
            if 500<=e.code<600:
                time.sleep(min(10,2**a)); continue
            return last
        except (URLError,TimeoutError,Exception) as e:
            last={'status':None,'url':url,'error':repr(e)}; time.sleep(min(10,2**a))
    return last

def q(vals,p):
    a=sorted(x for x in vals if x is not None and math.isfinite(x))
    if not a:return None
    if len(a)==1:return a[0]
    z=(len(a)-1)*p; lo=int(math.floor(z)); hi=int(math.ceil(z))
    return a[lo] if lo==hi else a[lo]+(a[hi]-a[lo])*(z-lo)

def pearson(xs,ys):
    pairs=[(x,y) for x,y in zip(xs,ys) if x is not None and y is not None and math.isfinite(x) and math.isfinite(y)]
    if len(pairs)<3:return None
    ax=sum(x for x,_ in pairs)/len(pairs); ay=sum(y for _,y in pairs)/len(pairs)
    num=sum((x-ax)*(y-ay) for x,y in pairs)
    dx=sum((x-ax)**2 for x,_ in pairs); dy=sum((y-ay)**2 for _,y in pairs)
    return num/math.sqrt(dx*dy) if dx>0 and dy>0 else None

def write_csv(path,rows):
    if not rows: open(path,'w').close(); return
    keys=[]; seen=set()
    for r in rows:
        for k in r:
            if k not in seen: seen.add(k); keys.append(k)
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=keys); w.writeheader(); w.writerows(rows)

def activity_all(wallet,start,end,errors):
    out=[]; seen=set(); pages=0; capped_pages=0
    for page in range(200):
        off=page*ACTIVITY_PAGE
        r=get('https://data-api.polymarket.com/activity',{'user':wallet,'type':'MAKER_REBATE','limit':ACTIVITY_PAGE,'offset':off,'start':start,'end':end})
        if r.get('status')!=200:
            errors.append({'stage':'activity','wallet':wallet,'offset':off,'response':r}); break
        rows=items(r.get('data')); pages+=1
        if len(rows)==ACTIVITY_PAGE: capped_pages+=1
        for e in rows:
            t=ets(e)
            if t is not None and not (start<=t<=end): continue
            key=(first(e,'transactionHash','transaction_hash'),t,fnum(first(e,'usdcSize','usdc_size','amount','size')),first(e,'type'))
            if key in seen: continue
            seen.add(key); out.append(e)
        if len(rows)<ACTIVITY_PAGE: break
        known=[ets(e) for e in rows if ets(e) is not None]
        if known and min(known)<start: break
    return out,pages,capped_pages

def leaderboard_user(wallet,category,period='MONTH'):
    r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':category,'timePeriod':period,'orderBy':'VOL','user':wallet,'limit':5,'offset':0})
    rows=items(r.get('data')) if r.get('status')==200 else []
    return r,(rows[0] if rows else None)

def trade_key(t):
    return (str(first(t,'transactionHash','transaction_hash') or ''),str(first(t,'proxyWallet','proxy_wallet') or '').lower(),str(first(t,'conditionId','condition_id') or ''),str(first(t,'asset') or ''),str(first(t,'side') or ''),round(fnum(first(t,'size')),10),round(fnum(first(t,'price')),10),ets(t))

def taker_only_probe(candidate_wallets,now_ts):
    windows=[3600,6*3600,24*3600,3*24*3600]
    trials=[]
    for w in candidate_wallets[:20]:
        for window in windows:
            params={'user':w,'limit':500,'offset':0,'start':now_ts-window,'end':now_ts}
            rt=get('https://data-api.polymarket.com/trades',{**params,'takerOnly':'true'})
            rf=get('https://data-api.polymarket.com/trades',{**params,'takerOnly':'false'})
            tr=items(rt.get('data')) if rt.get('status')==200 else []
            fr=items(rf.get('data')) if rf.get('status')==200 else []
            ct,cf=Counter(map(trade_key,tr)),Counter(map(trade_key,fr))
            missing=ct-cf; extras=cf-ct
            all_fields=sorted({k for row in fr[:50] if isinstance(row,dict) for k in row})
            role_fields=[k for k in all_fields if any(s in k.lower() for s in ('maker','taker','role','trader'))]
            rec={'wallet':w,'window_seconds':window,'true_status':rt.get('status'),'false_status':rf.get('status'),'true_rows':len(tr),'false_rows':len(fr),'true_subset_false':sum(missing.values())==0,'maker_candidate_rows_by_multiset_diff':sum(extras.values()),'role_like_fields_in_public_rows':role_fields,'sample_false_keys':all_fields,'sample_maker_candidate':next(iter(extras),None)}
            trials.append(rec)
            if 0<len(fr)<500 and len(tr)<500 and rec['true_subset_false'] and rec['maker_candidate_rows_by_multiset_diff']>0:
                rec['verdict']='PUBLIC_DATA_API_CAN_CLASSIFY_USER_MAKER_ROWS_BY_FALSE_MINUS_TRUE'
                return {'best':rec,'trials':trials}
    return {'best':None,'trials':trials,'verdict':'INCONCLUSIVE_OR_CAPPED'}

def rebate_date_probe():
    # Known activity payment: 2026-08-24 00:45 UTC, 554.0145 USDC.
    out={'wallet':KNOWN_WALLET,'activity_payment_date':'2026-08-24','activity_amount':554.0145}
    for d in ('2026-08-24','2026-08-23'):
        r=get('https://clob.polymarket.com/rebates/current',{'date':d,'maker_address':KNOWN_WALLET})
        rows=items(r.get('data'))
        out[d]={'status':r.get('status'),'wire_json_type':r.get('raw_type'),'rows':len(rows),'sum':sum(fnum(first(x,'rebated_fees_usdc','rebatedFeesUsdc','earnings')) for x in rows) if r.get('status')==200 else None,'prefix':r.get('prefix')}
    a=out['2026-08-24']; p=out['2026-08-23']
    if p['sum'] is not None and abs(p['sum']-554.0145)<0.01:
        out['verdict']='ACTIVITY_PAYMENT_DATE_EQUALS_TRADING_DATE_PLUS_ONE_DAY'
    else: out['verdict']='NOT_RECONCILED'
    return out

def positions_all(wallet,endpoint,limit=100,max_pages=120):
    out=[]
    for page in range(max_pages):
        r=get('https://data-api.polymarket.com/'+endpoint,{'user':wallet,'limit':limit,'offset':page*limit})
        if r.get('status')!=200: return out,r
        rows=items(r.get('data')); out.extend(rows)
        if len(rows)<limit: return out,r
    return out,{'status':200,'warning':'page_cap'}

def pnl_basis_probe(rows):
    # Choose wallets with largest absolute current open-position cash PnL among a small sample.
    candidates=[]
    for row in rows[:20]:
        w=row['wallet']
        pos,r=positions_all(w,'positions',limit=100,max_pages=10)
        if r.get('status')!=200: continue
        open_cash=sum(fnum(first(x,'cashPnl','cash_pnl')) for x in pos)
        open_real=sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in pos)
        candidates.append((abs(open_cash),w,pos,open_cash,open_real))
    candidates.sort(reverse=True,key=lambda x:x[0])
    probes=[]
    for _,w,pos,open_cash,open_real in candidates[:3]:
        lr,lb=leaderboard_user(w,'OVERALL','ALL')
        closed,cr=positions_all(w,'closed-positions',limit=100,max_pages=120)
        if not lb or cr.get('status')!=200: continue
        closed_real=sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in closed)
        pnl=fnum(first(lb,'pnl'))
        realized_candidate=closed_real+open_real
        mtm_candidate=closed_real+open_cash
        probes.append({'wallet':w,'leaderboard_overall_all_pnl':pnl,'closed_realized_sum':closed_real,'open_realized_sum':open_real,'open_cash_pnl_sum':open_cash,'realized_candidate':realized_candidate,'mtm_candidate':mtm_candidate,'abs_error_realized_candidate':abs(pnl-realized_candidate),'abs_error_mtm_candidate':abs(pnl-mtm_candidate),'open_positions':len(pos),'closed_positions':len(closed)})
    if probes:
        mtm_wins=sum(1 for p in probes if p['abs_error_mtm_candidate']<p['abs_error_realized_candidate'])
        verdict='MTM_LIKELY' if mtm_wins==len(probes) else ('REALIZED_LIKELY' if mtm_wins==0 else 'MIXED_OR_ACCOUNTING_DIFFERENCES')
    else: verdict='INCONCLUSIVE'
    return {'verdict':verdict,'probes':probes,'caveat':'Position cashPnl/realizedPnl and leaderboard ALL may differ for rebates, transfers, splits/merges, and legacy accounting; this is an empirical basis check, not formal accounting reconciliation.'}

def main():
    now=datetime.now(timezone.utc); end=int(now.timestamp()); start=int((now-timedelta(days=LOOKBACK_DAYS)).timestamp())
    errors=[]; crypto=[]
    for off in range(0,MAX_CRYPTO_LEADERBOARD,50):
        r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':'CRYPTO','timePeriod':'MONTH','orderBy':'VOL','limit':50,'offset':off})
        if r.get('status')!=200: errors.append({'stage':'crypto_leaderboard','offset':off,'response':r}); break
        rr=items(r.get('data')); crypto.extend(rr)
        if len(rr)<50: break
    by_wallet={}
    for x in crypto:
        w=first(x,'proxyWallet','proxy_wallet')
        if isinstance(w,str) and w.startswith('0x') and w not in by_wallet: by_wallet[w]=x

    def one(w):
        evs,pages,capped=activity_all(w,start,end,errors)
        lr,overall=leaderboard_user(w,'OVERALL','MONTH')
        return w,evs,pages,capped,lr,overall

    rows=[]; wallets=list(by_wallet)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(one,w) for w in wallets]
        for i,f in enumerate(as_completed(futs),1):
            w,evs,pages,capped,lr,overall=f.result()
            if lr.get('status')!=200: errors.append({'stage':'overall_leaderboard','wallet':w,'response':lr})
            if not evs or not overall: continue
            c=by_wallet[w]
            rebate=sum(fnum(first(e,'usdcSize','usdc_size','amount','size')) for e in evs)
            ovol=fnum(first(overall,'vol')); opnl=fnum(first(overall,'pnl'))
            cvol=fnum(first(c,'vol')); cpnl=fnum(first(c,'pnl'))
            try:crank=int(first(c,'rank'))
            except Exception:crank=None
            rows.append({'wallet':w,'crypto_rank':crank,'crypto_vol_month':cvol,'crypto_pnl_month':cpnl,'crypto_pnl_over_vol':cpnl/cvol if cvol else None,'overall_vol_month':ovol,'overall_pnl_month':opnl,'overall_pnl_over_vol':opnl/ovol if ovol else None,'maker_rebate_walletwide_31d':rebate,'rebate_over_overall_vol':rebate/ovol if ovol else None,'rebate_events_31d':len(evs),'rebate_days_31d':len({edate(e) for e in evs if edate(e)}),'activity_pages':pages,'full_activity_pages':capped})
            if i%50==0: print(f'processed={i}/{len(wallets)} usable={len(rows)}',flush=True)
    rows.sort(key=lambda r:(r['rebate_over_overall_vol'] if r['rebate_over_overall_vol'] is not None else -1),reverse=True)

    # Intensity deciles on category-consistent OVERALL denominators.
    valid=[r for r in rows if r['rebate_over_overall_vol'] is not None and r['overall_pnl_over_vol'] is not None]
    bins=[]
    n=len(valid)
    for b in range(10):
        lo=b*n//10; hi=(b+1)*n//10
        chunk=valid[lo:hi]
        vals=[x['overall_pnl_over_vol'] for x in chunk]
        bins.append({'intensity_decile_high_to_low':b+1,'n':len(chunk),'rebate_over_overall_vol_median':q([x['rebate_over_overall_vol'] for x in chunk],.5),'pnl_over_vol_median':q(vals,.5),'abs_pnl_over_vol_median':q([abs(x) for x in vals],.5),'p10':q(vals,.1),'p90':q(vals,.9),'p90_minus_p10':(q(vals,.9)-q(vals,.1)) if vals else None})
    xs=[r['rebate_over_overall_vol'] for r in valid]; ys=[r['overall_pnl_over_vol'] for r in valid]
    corr_abs=pearson(xs,[abs(y) for y in ys])
    corr_signed=pearson(xs,ys)

    date_probe=rebate_date_probe()
    trade_probe=taker_only_probe([KNOWN_WALLET]+[r['wallet'] for r in rows],end)
    basis_probe=pnl_basis_probe(rows)

    summary={'started_at':now.isoformat(),'finished_at':datetime.now(timezone.utc).isoformat(),'crypto_top500_wallets':len(by_wallet),'rebate_active_with_overall_metrics':len(rows),'errors':len(errors),'category_fix':'wallet-wide MAKER_REBATE divided by OVERALL leaderboard vol; compared with OVERALL leaderboard pnl/vol. Crypto pnl/vol is retained separately and never added to wallet-wide rebates.','activity_pagination':{'page_size':ACTIVITY_PAGE,'wallets_with_more_than_one_page':sum(1 for r in rows if r['activity_pages']>1),'wallets_with_at_least_one_full_100_row_page':sum(1 for r in rows if r['full_activity_pages']>0),'max_pages_seen':max([r['activity_pages'] for r in rows],default=0),'max_events_seen':max([r['rebate_events_31d'] for r in rows],default=0)},'scatter_overall':{'n':len(valid),'pearson_rebate_intensity_vs_signed_pnl_over_vol':corr_signed,'pearson_rebate_intensity_vs_abs_pnl_over_vol':corr_abs,'intensity_deciles_high_to_low':bins},'rebate_date_semantics':date_probe,'takerOnly_probe':trade_probe,'leaderboard_pnl_basis_probe':basis_probe,'notes':['OVERALL fixes the category mismatch but rebate/vol is not directly convertible to maker-share because category rebate percentages differ.','If takerOnly=false minus takerOnly=true cleanly yields maker rows, the on-chain OrderFilled decoder is unnecessary for maker/taker attribution in this public-data research path.']}
    write_csv('maker_intensity_overall.csv',rows)
    with open('maker_intensity_summary.json','w') as f: json.dump(summary,f,indent=2,default=str)
    with open('maker_intensity_errors.json','w') as f: json.dump(errors,f,indent=2,default=str)
    print('=== MAKER INTENSITY PROBE ==='); print(json.dumps(summary,indent=2,default=str)); print('FILES=maker_intensity_overall.csv,maker_intensity_summary.json,maker_intensity_errors.json')
    return 0

if __name__=='__main__': sys.exit(main())
