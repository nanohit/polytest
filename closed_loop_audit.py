#!/usr/bin/env python3
import csv, json, math, statistics, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA='nanohit-polytest-closed-loop/0.1'
TOPN=500
LOOKBACK_DAYS=31
D1_N=43
AUDIT_WALLETS=8
DAYS_PER_WALLET=2
TRADE_LIMIT=10000
ALLOWED_PRODUCTS={
    'crypto':0.07*0.20,
    'sports':0.05*0.15,
    'finance_politics_mentions_tech':0.04*0.25,
    'economics_culture_weather_general':0.05*0.25,
}


def fnum(x,default=0.0):
    try:return default if x in (None,'') else float(x)
    except:return default

def first(d,*keys):
    if not isinstance(d,dict):return None
    for k in keys:
        v=d.get(k)
        if v not in (None,''):return v
    return None

def items(x):
    if isinstance(x,list):return x
    if isinstance(x,dict):
        for k in ('data','results','activity','positions','trades'):
            if isinstance(x.get(k),list):return x[k]
    return []

def ets(ev):
    x=first(ev,'timestamp','time','createdAt','created_at')
    if x is None:return None
    try:
        if isinstance(x,(int,float)) or (isinstance(x,str) and x.isdigit()):
            n=int(x);return n//1000 if n>10_000_000_000 else n
        return int(datetime.fromisoformat(str(x).replace('Z','+00:00')).timestamp())
    except:return None

def get(base,params=None,retries=5,timeout=40):
    url=base+(('?'+urlencode(params,doseq=True)) if params else '')
    last=None
    for a in range(retries):
        try:
            req=Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
            with urlopen(req,timeout=timeout) as r:
                raw=r.read().decode('utf-8','replace')
                try:data=json.loads(raw)
                except:data=None
                return {'status':r.status,'data':data,'raw_type':type(data).__name__,'url':url,'prefix':raw[:250]}
        except HTTPError as e:
            raw=e.read().decode('utf-8','replace')
            try:data=json.loads(raw)
            except:data=None
            last={'status':e.code,'data':data,'raw_type':type(data).__name__,'url':url,'prefix':raw[:250]}
            if e.code==429:
                ra=(data.get('retry_after_seconds') if isinstance(data,dict) else None) or e.headers.get('Retry-After')
                try:w=max(.5,min(20,float(ra)))
                except:w=min(20,1.5*2**a)
                time.sleep(w);continue
            if 500<=e.code<600:
                time.sleep(min(10,2**a));continue
            return last
        except (URLError,TimeoutError,Exception) as e:
            last={'status':None,'url':url,'error':repr(e)};time.sleep(min(10,2**a))
    return last

def activity_all(wallet,start,end):
    out=[];off=0
    while off<=10000:
        r=get('https://data-api.polymarket.com/activity',{'user':wallet,'type':'MAKER_REBATE','limit':100,'offset':off,'start':start,'end':end})
        if r.get('status')!=200:return out,r
        rr=items(r.get('data'));out.extend(rr)
        if len(rr)<100:return out,r
        off+=100
    return out,{'status':200,'warning':'offset_cap'}

def leaderboard_user(wallet,category='OVERALL',period='MONTH'):
    r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':category,'timePeriod':period,'orderBy':'VOL','user':wallet,'limit':5,'offset':0})
    rr=items(r.get('data')) if r.get('status')==200 else []
    return (rr[0] if rr else None),r

def build_live_cohort(now):
    crypto=[]
    for off in range(0,TOPN,50):
        r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':'CRYPTO','timePeriod':'MONTH','orderBy':'VOL','limit':50,'offset':off})
        if r.get('status')!=200:break
        rr=items(r.get('data'));crypto.extend(rr)
        if len(rr)<50:break
    start=int((now-timedelta(days=LOOKBACK_DAYS)).timestamp());end=int(now.timestamp())
    rows=[]
    for i,c in enumerate(crypto,1):
        w=first(c,'proxyWallet','proxy_wallet')
        if not w:continue
        evs,_=activity_all(w,start,end)
        if not evs:continue
        o,_=leaderboard_user(w,'OVERALL','MONTH')
        if not o:continue
        ov=fnum(first(o,'vol'));op=fnum(first(o,'pnl'));cv=fnum(first(c,'vol'));cp=fnum(first(c,'pnl'))
        reb=sum(fnum(first(e,'usdcSize','usdc_size','amount','size')) for e in evs)
        rows.append({'wallet':w.lower(),'crypto_vol':cv,'crypto_pnl':cp,'overall_vol':ov,'overall_pnl':op,'rebate':reb,'intensity':reb/ov if ov else None,'crypto_share':cv/ov if ov else None,'events':evs})
        if i%100==0:print('cohort',i,len(rows),flush=True)
    rows=[r for r in rows if r['intensity'] is not None]
    rows.sort(key=lambda r:r['intensity'],reverse=True)
    return rows

def trade_key(t):
    return (
        str(first(t,'transactionHash','transaction_hash') or '').lower(),
        str(first(t,'conditionId','condition_id') or '').lower(),
        str(first(t,'asset') or ''),str(first(t,'side') or ''),
        format(fnum(first(t,'size')),'.12g'),format(fnum(first(t,'price')),'.12g'),str(ets(t))
    )

def fetch_trade_slice(wallet,start,end,taker_only,depth=0):
    if end<start:return []
    r=get('https://data-api.polymarket.com/trades',{'user':wallet,'takerOnly':'true' if taker_only else 'false','limit':TRADE_LIMIT,'offset':0,'start':start,'end':end})
    if r.get('status')!=200:raise RuntimeError('trade fetch failed '+str(r))
    rr=items(r.get('data'))
    if len(rr)<TRADE_LIMIT:return rr
    if depth>=20 or end-start<=1:
        raise RuntimeError(f'trade slice still capped wallet={wallet} {start}-{end} rows={len(rr)}')
    mid=(start+end)//2
    a=fetch_trade_slice(wallet,start,mid,taker_only,depth+1)
    b=fetch_trade_slice(wallet,mid+1,end,taker_only,depth+1)
    return a+b

def maker_diff(wallet,start,end):
    all_rows=fetch_trade_slice(wallet,start,end,False)
    taker_rows=fetch_trade_slice(wallet,start,end,True)
    ca=Counter(map(trade_key,all_rows));ct=Counter(map(trade_key,taker_rows));extra=ca-ct;missing=ct-ca
    exemplar={trade_key(t):t for t in all_rows}
    maker=[]
    for k,n in extra.items():
        maker.extend([exemplar[k]]*n)
    return all_rows,taker_rows,maker,sum(missing.values())

def rebate_current(wallet,d):
    r=get('https://clob.polymarket.com/rebates/current',{'date':d,'maker_address':wallet})
    rr=items(r.get('data')) if r.get('status')==200 else []
    return rr,r

def fee_rate_probe(asset):
    probes=[]
    for url,params in [
        ('https://clob.polymarket.com/fee-rate',{'token_id':asset}),
        ('https://clob.polymarket.com/fee-rate/'+str(asset),None),
    ]:
        r=get(url,params);probes.append({'url':r.get('url'),'status':r.get('status'),'data':r.get('data'),'prefix':r.get('prefix')})
        if r.get('status')==200:return probes
    return probes

def closed_loop_wallet_day(wallet,d,activity_amount=None):
    day=datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
    start=int(day.timestamp());end=int((day+timedelta(days=1)).timestamp())-1
    all_rows,taker_rows,maker_rows,missing=maker_diff(wallet,start,end)
    rebate_rows,rr=rebate_current(wallet,d)
    actual_total=sum(fnum(first(x,'rebated_fees_usdc','rebatedFeesUsdc','earnings')) for x in rebate_rows)
    byc=defaultdict(lambda:{'base':0.0,'notional':0.0,'fills':0,'assets':set()})
    for t in maker_rows:
        c=str(first(t,'conditionId','condition_id') or '').lower();p=fnum(first(t,'price'));C=fnum(first(t,'size'))
        byc[c]['base']+=C*p*(1-p);byc[c]['notional']+=C*p;byc[c]['fills']+=1;byc[c]['assets'].add(str(first(t,'asset') or ''))
    actual_c=defaultdict(float)
    for x in rebate_rows:
        actual_c[str(first(x,'condition_id','conditionId') or '').lower()]+=fnum(first(x,'rebated_fees_usdc','rebatedFeesUsdc','earnings'))
    cond=[]
    for c,a in actual_c.items():
        b=byc.get(c,{'base':0.0,'notional':0.0,'fills':0,'assets':set()})
        ratio=a/b['base'] if b['base']>0 else None
        nearest=None;relerr=None
        if ratio is not None:
            nearest=min(ALLOWED_PRODUCTS.items(),key=lambda kv:abs(kv[1]-ratio))
            relerr=abs(ratio-nearest[1])/nearest[1] if nearest[1] else None
        cond.append({'condition_id':c,'actual_rebate':a,'reconstructed_fee_curve_base':b['base'],'maker_notional':b['notional'],'maker_fills':b['fills'],'actual_over_base':ratio,'nearest_current_product':nearest[0] if nearest else None,'nearest_product_value':nearest[1] if nearest else None,'relative_error_to_nearest_product':relerr,'asset_sample':next(iter(b['assets']),None)})
    matched_actual=sum(x['actual_rebate'] for x in cond if x['reconstructed_fee_curve_base']>0)
    good=[x for x in cond if x['relative_error_to_nearest_product'] is not None and x['relative_error_to_nearest_product']<=0.03]
    total_notional=sum(fnum(first(t,'size'))*fnum(first(t,'price')) for t in all_rows)
    maker_notional=sum(fnum(first(t,'size'))*fnum(first(t,'price')) for t in maker_rows)
    sample_asset=next((str(first(t,'asset')) for t in maker_rows if first(t,'asset')),None)
    return {
        'wallet':wallet,'trade_date':d,'all_trade_rows':len(all_rows),'taker_rows':len(taker_rows),'maker_rows':len(maker_rows),'taker_not_subset_missing':missing,
        'all_user_notional':total_notional,'maker_notional':maker_notional,'maker_share_of_user_notional':maker_notional/total_notional if total_notional else None,
        'rebates_current_wire_type':rr.get('raw_type'),'rebate_rows':len(rebate_rows),'actual_rebate_total':actual_total,'activity_payment_amount_next_day':activity_amount,
        'activity_vs_current_abs_error':abs(actual_total-activity_amount) if activity_amount is not None else None,
        'actual_rebate_covered_by_reconstructed_conditions':matched_actual/actual_total if actual_total else None,
        'conditions_with_actual_rebate':len(cond),'conditions_with_ratio_within_3pct_of_allowed_current_product':len(good),
        'condition_match_fraction':len(good)/len(cond) if cond else None,
        'fee_rate_endpoint_probe':fee_rate_probe(sample_asset) if sample_asset else [],
        'conditions':cond,
    }

def closed_positions_since(wallet,start_ts):
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

def pnl_period_probe(wallet,now):
    lbm,_=leaderboard_user(wallet,'OVERALL','MONTH')
    if not lbm:return {'wallet':wallet,'error':'no leaderboard month'}
    # Test both plausible month windows because docs do not define calendar vs rolling semantics.
    starts={
        'rolling_30d':int((now-timedelta(days=30)).timestamp()),
        'calendar_month':int(datetime(now.year,now.month,1,tzinfo=timezone.utc).timestamp()),
    }
    out={'wallet':wallet,'leaderboard_month_pnl':fnum(first(lbm,'pnl')),'leaderboard_month_vol':fnum(first(lbm,'vol')),'windows':{}}
    for name,st in starts.items():
        cp,r=closed_positions_since(wallet,st)
        out['windows'][name]={'closed_positions':len(cp),'closed_realized_sum':sum(fnum(first(x,'realizedPnl','realized_pnl')) for x in cp),'oldest_timestamp':min([ets(x) for x in cp if ets(x) is not None],default=None),'status':r.get('status'),'warning':r.get('warning')}
    return out

def med(v):
    v=[x for x in v if x is not None and math.isfinite(x)]
    return statistics.median(v) if v else None

def main():
    now=datetime.now(timezone.utc)
    cohort=build_live_cohort(now)
    d1=cohort[:D1_N]
    d1_stats={
        'n':len(d1),'median_trading_pnl':med([r['overall_pnl'] for r in d1]),'median_rebate':med([r['rebate'] for r in d1]),
        'negative_trading_pnl_count':sum(r['overall_pnl']<0 for r in d1),
        'negative_then_positive_if_rebate_added':sum(r['overall_pnl']<0 and r['overall_pnl']+r['rebate']>0 for r in d1),
        'still_negative_if_rebate_added':sum(r['overall_pnl']+r['rebate']<0 for r in d1),
        'rebate_gt_abs_pnl_count':sum(r['rebate']>abs(r['overall_pnl']) for r in d1),
        'median_rebate_over_rebate_plus_abs_pnl':med([r['rebate']/(r['rebate']+abs(r['overall_pnl'])) for r in d1 if r['rebate']+abs(r['overall_pnl'])>0]),
        'ratio_of_medians_loss_to_rebate':(-med([r['overall_pnl'] for r in d1])/med([r['rebate'] for r in d1])) if med([r['rebate'] for r in d1]) else None,
        'median_signed_loss_over_rebate':med([-r['overall_pnl']/r['rebate'] for r in d1 if r['rebate']>0]),
        'median_loss_over_rebate_among_losers':med([-r['overall_pnl']/r['rebate'] for r in d1 if r['rebate']>0 and r['overall_pnl']<0]),
        'aggregate_pnl_over_rebate':sum(r['overall_pnl'] for r in d1)/sum(r['rebate'] for r in d1),
        'crypto_share_median':med([r['crypto_share'] for r in cohort]),'crypto_share_lt_50pct':sum((r['crypto_share'] or 0)<0.5 for r in cohort),
        'cohort_overall_vol':sum(r['overall_vol'] for r in cohort),'cohort_overall_pnl':sum(r['overall_pnl'] for r in cohort),'cohort_rebate':sum(r['rebate'] for r in cohort),
    }
    audits=[]
    for r in d1[:AUDIT_WALLETS]:
        # activity payment date P corresponds empirically to trading date P-1.
        bypay=defaultdict(float)
        for e in r['events']:
            t=ets(e)
            if t is None:continue
            pd=datetime.fromtimestamp(t,tz=timezone.utc).date()
            if pd>=now.date():continue
            bypay[pd]+=fnum(first(e,'usdcSize','usdc_size','amount','size'))
        pays=sorted(bypay,reverse=True)[:DAYS_PER_WALLET]
        for payd in pays:
            trade_d=(payd-timedelta(days=1)).isoformat()
            try:
                a=closed_loop_wallet_day(r['wallet'],trade_d,bypay[payd]);a['intensity_rank_within_cohort']=cohort.index(r)+1;audits.append(a)
                print('closed-loop',r['wallet'],trade_d,'maker',a['maker_rows'],'actual',a['actual_rebate_total'],'match',a['condition_match_fraction'],flush=True)
            except Exception as e:
                audits.append({'wallet':r['wallet'],'trade_date':trade_d,'error':repr(e)})
    # PnL basis on several high-rebate D1 wallets; full timestamp pagination for closed positions in period.
    pnl_probes=[pnl_period_probe(r['wallet'],now) for r in d1[:5]]
    summary={
        'started_finished_at':now.isoformat(),'cohort_n':len(cohort),'d1_stats':d1_stats,
        'current_fee_rebate_products_assumed_from_docs':ALLOWED_PRODUCTS,
        'closed_loop_audits':audits,
        'pnl_period_probes':pnl_probes,
        'interpretation_rules':{
            'closed_loop_pass':'For actual rebate-bearing conditions, actual_rebate / sum(C*p*(1-p)) should land near one of independently known current feeRate*makerRebate products, with high actual-rebate coverage and taker rows a subset of all rows.',
            'pnl_probe':'closed.realizedPnl is only one accounting component. Agreement/disagreement is diagnostic, not proof, unless period semantics and all components reconcile.'
        }
    }
    with open('closed_loop_audit.json','w') as f:json.dump(summary,f,indent=2,default=str)
    # Flatten condition diagnostics for easy inspection.
    flat=[]
    for a in audits:
        for c in a.get('conditions',[]):flat.append({k:a.get(k) for k in ('wallet','trade_date','maker_rows','actual_rebate_total','maker_share_of_user_notional')}|c)
    if flat:
        with open('closed_loop_conditions.csv','w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(flat[0]));w.writeheader();w.writerows(flat)
    else:open('closed_loop_conditions.csv','w').close()
    print('=== CLOSED LOOP AUDIT ===')
    print(json.dumps({'d1_stats':d1_stats,'audits':[{k:a.get(k) for k in ('wallet','trade_date','maker_rows','taker_rows','actual_rebate_total','actual_rebate_covered_by_reconstructed_conditions','condition_match_fraction','maker_share_of_user_notional','error')} for a in audits],'pnl_period_probes':pnl_probes},indent=2,default=str))
    return 0

if __name__=='__main__':sys.exit(main())
