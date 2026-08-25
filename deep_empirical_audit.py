#!/usr/bin/env python3
import csv, json, math, statistics, time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from closed_loop_audit import (
    get, items, first, fnum, ets, activity_all,
    closed_loop_wallet_day, rebate_current,
)

UTC = timezone.utc
TRADE_LIMIT = 10000

# Wallets whose month-level maker share was empirically reconstructed in run #20.
# Most are deliberately mixed maker/taker candidates rather than pure makers.
MIXED_CANDIDATES = [
    '0x7df1854c8bd10b573f1281c2e978b8104075491d',
    '0x337b7fca244bf2ef3272469ee02a2bd541097e26',
    '0x388537259dc9e693c1c9b96fdf07a63f6b7aca77',
    '0x252d7bae5ec87553dcafb23e275c202ac0d3c456',
    '0xe9ed54623043c0e5d6380270f07f0e044c884543',
    '0x91aab8d0ae5c7dde7d9ea7ca49aeb985e09819b8',
    '0x44832d0d2ec11187c1e77d786feb15f6a50254c6',
    '0x239e726f1f4131ca78a7e5abf1fec86bfc151c10',
    '0xa5e9072668d0acda2455869f4244ddbb9edd74c6',
    '0x2e31da14fb1ee55a1de99b1caebcbf7ed895d5fd',
    '0x67e38fc4bbf6b8929df424b1a95e7c3c4e392a16',
    '0x13f0bcec1e2e60ec9acc3bee4d2da2fe9694a50f',
    # Extra high-activity wallets from earlier closed-loop probes.
    '0x48fee9f5a5fc6fd6b8299356ed03740ff89178ef',
    '0x6f398b37f0f0df01b8f85f6379edb1081db25341',
]

COMPETITION_WALLETS = [
    '0xf3326d8fc30f7ed01bfade12c706cb52c397b1c0',
    '0x673d4961db71c47fc4db14c41e42779a854b5234',
    '0x7df1854c8bd10b573f1281c2e978b8104075491d',
    '0x337b7fca244bf2ef3272469ee02a2bd541097e26',
    '0x252d7bae5ec87553dcafb23e275c202ac0d3c456',
    '0xe9ed54623043c0e5d6380270f07f0e044c884543',
]

_clob_cache = {}
_gamma_cache = {}


def med(xs):
    xs=[x for x in xs if x is not None and math.isfinite(x)]
    return statistics.median(xs) if xs else None


def corr(xs, ys):
    pairs=[(x,y) for x,y in zip(xs,ys) if x is not None and y is not None and math.isfinite(x) and math.isfinite(y)]
    if len(pairs)<3:return None
    x=[a for a,_ in pairs]; y=[b for _,b in pairs]
    mx=sum(x)/len(x); my=sum(y)/len(y)
    dx=sum((a-mx)**2 for a in x); dy=sum((b-my)**2 for b in y)
    if dx<=0 or dy<=0:return None
    return sum((a-mx)*(b-my) for a,b in pairs)/math.sqrt(dx*dy)


def clob_info(cid):
    cid=cid.lower()
    if cid not in _clob_cache:
        r=get('https://clob.polymarket.com/clob-markets/'+cid)
        _clob_cache[cid]=r.get('data') if r.get('status')==200 and isinstance(r.get('data'),dict) else {}
    return _clob_cache[cid]


def gamma_market(cid):
    cid=cid.lower()
    if cid not in _gamma_cache:
        r=get('https://gamma-api.polymarket.com/markets',{'condition_ids':cid,'limit':5})
        rr=items(r.get('data')) if r.get('status')==200 else []
        _gamma_cache[cid]=rr[0] if rr else {}
    return _gamma_cache[cid]


def fee_rate_endpoint(asset):
    if not asset:return None
    r=get('https://clob.polymarket.com/fee-rate',{'token_id':str(asset)})
    return {'status':r.get('status'),'data':r.get('data'),'wire_type':r.get('raw_type'),'prefix':r.get('prefix')}


def payment_trade_dates(wallet, lookback_days=24):
    now=datetime.now(UTC)
    evs,_=activity_all(wallet,int((now-timedelta(days=lookback_days)).timestamp()),int(now.timestamp()))
    out=[]; seen=set()
    for e in sorted(evs,key=lambda z:ets(z) or 0,reverse=True):
        t=ets(e)
        if t is None:continue
        pay=datetime.fromtimestamp(t,UTC).date()
        d=pay-timedelta(days=1)  # empirically validated payment D+1 -> trading day D
        if d>=now.date() or d in seen:continue
        seen.add(d)
        out.append((d.isoformat(),fnum(first(e,'usdcSize','amount','size')),pay.isoformat()))
    return out


def condition_base_from_maker_rows(maker_rows):
    out=defaultdict(lambda:{'base':0.0,'notional':0.0,'fills':0,'assets':set()})
    for t in maker_rows:
        cid=str(first(t,'conditionId','condition_id') or '').lower()
        p=fnum(first(t,'price')); C=fnum(first(t,'size'))
        out[cid]['base'] += C*p*(1-p)
        out[cid]['notional'] += C*p
        out[cid]['fills'] += 1
        if first(t,'asset') is not None: out[cid]['assets'].add(str(first(t,'asset')))
    return out


def validate_condition_product(cond):
    cid=cond['condition_id']
    info=clob_info(cid)
    fd=info.get('fd') if isinstance(info,dict) else None
    rate=fnum(fd.get('r')) if isinstance(fd,dict) else 0.0
    ratio=cond.get('actual_over_base')
    inferred=(ratio/rate) if ratio is not None and rate>0 else None
    return {
        'fd_rate':rate,
        'fd_exponent':fd.get('e') if isinstance(fd,dict) else None,
        'fd_taker_only':fd.get('to') if isinstance(fd,dict) else None,
        'inferred_rebate_share':inferred,
        'ratio':ratio,
        'category':first(gamma_market(cid),'category'),
        'title':first(gamma_market(cid),'question','title'),
    }


def mixed_wallet_day_audit(target_n=25):
    selected=[]; scanned=[]
    for w in MIXED_CANDIDATES:
        dates=payment_trade_dates(w,24)[:7]
        for d,activity_amt,paydate in dates:
            try:
                a=closed_loop_wallet_day(w,d,activity_amt)
            except Exception as e:
                scanned.append({'wallet':w,'trade_date':d,'error':repr(e)})
                continue
            share=a.get('maker_share_of_user_notional')
            row={k:a.get(k) for k in [
                'wallet','trade_date','all_trade_rows','taker_rows','maker_rows','taker_not_subset_missing',
                'all_user_notional','maker_notional','maker_share_of_user_notional','actual_rebate_total',
                'actual_rebate_covered_by_reconstructed_conditions','conditions_with_actual_rebate'
            ]}
            row['activity_payment_date']=paydate; row['activity_payment_amount']=activity_amt
            scanned.append(row)
            if share is not None and 0.20 <= share <= 0.80 and a.get('actual_rebate_total',0)>0 and a.get('maker_rows',0)>0 and a.get('taker_rows',0)>0:
                # Validate the condition-level coefficient on the conditions carrying most money.
                conds=sorted(a.get('conditions',[]),key=lambda x:x.get('actual_rebate',0),reverse=True)
                validated=[]; cum=0.0; total=a.get('actual_rebate_total',0)
                for c in conds:
                    if c.get('reconstructed_fee_curve_base',0)<=0:continue
                    v=validate_condition_product(c)
                    v.update({'condition_id':c['condition_id'],'actual_rebate':c['actual_rebate'],'base':c['reconstructed_fee_curve_base']})
                    validated.append(v); cum += c['actual_rebate']
                    if len(validated)>=8 and (total<=0 or cum/total>=0.90):break
                row['validated_conditions']=validated
                row['validated_rebate_dollars']=sum(v['actual_rebate'] for v in validated)
                selected.append(row)
                print('MIXED',len(selected),w,d,'share',round(share,4),'rebate',round(a.get('actual_rebate_total',0),4),'maker/taker',a.get('maker_rows'),a.get('taker_rows'),flush=True)
                if len(selected)>=target_n:break
        if len(selected)>=target_n:break
    money=sum(r.get('actual_rebate_total',0) for r in selected)
    covered=sum(r.get('actual_rebate_total',0)*fnum(r.get('actual_rebate_covered_by_reconstructed_conditions')) for r in selected)
    vals=[v for r in selected for v in r.get('validated_conditions',[])]
    valid_money=sum(v['actual_rebate'] for v in vals if v.get('inferred_rebate_share') is not None)
    near_money=sum(v['actual_rebate'] for v in vals if v.get('inferred_rebate_share') is not None and min(abs(v['inferred_rebate_share']-.20),abs(v['inferred_rebate_share']-.25)) <= .01)
    return {
        'target_n':target_n,'selected_n':len(selected),'scanned_n':len(scanned),
        'selected_actual_rebate_dollars':money,
        'rebate_dollars_with_reconstructed_condition_base':covered,
        'money_coverage_fraction':covered/money if money else None,
        'validated_condition_dollars':valid_money,
        'validated_condition_dollars_near_20_or_25pct_share':near_money,
        'validated_share_money_fraction':near_money/valid_money if valid_money else None,
        'maker_share_median':med([r.get('maker_share_of_user_notional') for r in selected]),
        'selected':selected,'scanned':scanned,
    }


def market_trade_key(t):
    return (
        str(first(t,'proxyWallet','proxy_wallet') or '').lower(),
        str(first(t,'transactionHash','transaction_hash') or '').lower(),
        str(first(t,'conditionId','condition_id') or '').lower(), str(first(t,'asset') or ''),
        str(first(t,'side') or ''), format(fnum(first(t,'size')),'.12g'), format(fnum(first(t,'price')),'.12g'), str(ets(t))
    )


def fetch_market_slice(cid,start,end,taker_only,depth=0):
    r=get('https://data-api.polymarket.com/trades',{
        'market':cid,'takerOnly':'true' if taker_only else 'false','limit':TRADE_LIMIT,'offset':0,'start':start,'end':end
    })
    if r.get('status')!=200:raise RuntimeError('market trade fetch failed '+str(r))
    rr=items(r.get('data'))
    if len(rr)<TRADE_LIMIT:return rr
    if depth>=20 or end-start<=1:raise RuntimeError(f'market slice capped cid={cid} {start}-{end}')
    mid=(start+end)//2
    return fetch_market_slice(cid,start,mid,taker_only,depth+1)+fetch_market_slice(cid,mid+1,end,taker_only,depth+1)


def market_maker_stats(cid,d):
    day=datetime.fromisoformat(d).replace(tzinfo=UTC); start=int(day.timestamp());end=start+86399
    allr=fetch_market_slice(cid,start,end,False); taker=fetch_market_slice(cid,start,end,True)
    ca=Counter(map(market_trade_key,allr)); ct=Counter(map(market_trade_key,taker)); extra=ca-ct; missing=ct-ca
    ex={market_trade_key(t):t for t in allr}; maker=[]
    for k,n in extra.items():maker.extend([ex[k]]*n)
    wallets={str(first(t,'proxyWallet','proxy_wallet') or '').lower() for t in maker if first(t,'proxyWallet','proxy_wallet')}
    base=sum(fnum(first(t,'size'))*fnum(first(t,'price'))*(1-fnum(first(t,'price'))) for t in maker)
    notional=sum(fnum(first(t,'size'))*fnum(first(t,'price')) for t in maker)
    return {'all_rows':len(allr),'taker_rows':len(taker),'maker_rows':len(maker),'missing_taker_rows':sum(missing.values()),'active_filled_makers':len(wallets),'market_maker_base':base,'market_maker_notional':notional}


def quantile_pick(rows,n):
    if len(rows)<=n:return rows
    out=[]
    for i in range(n):
        j=round(i*(len(rows)-1)/(n-1)); out.append(rows[j])
    # unique condition ids
    seen=set(); final=[]
    for r in out:
        if r['condition_id'] not in seen: seen.add(r['condition_id']); final.append(r)
    return final


def competition_flatness_probe():
    today=datetime.now(UTC).date()
    best=None
    # Two finalized candidate days; choose the one producing the richest crypto condition set.
    for ago in (2,3,4):
        d=(today-timedelta(days=ago)).isoformat(); candidates=[]
        for w in COMPETITION_WALLETS:
            try:a=closed_loop_wallet_day(w,d,None)
            except Exception:continue
            for c in a.get('conditions',[]):
                if c.get('reconstructed_fee_curve_base',0)<=0 or c.get('actual_rebate',0)<=0:continue
                info=clob_info(c['condition_id']); fd=info.get('fd') if isinstance(info,dict) else None
                rate=fnum(fd.get('r')) if isinstance(fd,dict) else 0
                if abs(rate-.07)>.002:continue
                candidates.append({
                    'wallet':w,'trade_date':d,'condition_id':c['condition_id'],'actual_rebate':c['actual_rebate'],
                    'target_base':c['reconstructed_fee_curve_base'],'target_ratio':c['actual_over_base'],
                    'title':first(gamma_market(c['condition_id']),'question','title')
                })
        # one representative target wallet per condition, favor higher rebate
        by={}
        for r in candidates:
            cid=r['condition_id']
            if cid not in by or r['actual_rebate']>by[cid]['actual_rebate']:by[cid]=r
        cand=list(by.values()); cand.sort(key=lambda r:r['actual_rebate'])
        if best is None or len(cand)>len(best[1]):best=(d,cand)
    if not best:return {'error':'no competition candidates'}
    d,cand=best; picked=quantile_pick(cand,14); rows=[]
    for i,r in enumerate(picked,1):
        try:s=market_maker_stats(r['condition_id'],d)
        except Exception as e:
            r=dict(r);r['error']=repr(e);rows.append(r);continue
        r=dict(r);r.update(s)
        r['target_share_of_market_fee_curve_base']=r['target_base']/s['market_maker_base'] if s['market_maker_base'] else None
        rows.append(r)
        print('COMP',i,d,'makers',s['active_filled_makers'],'ratio',r['target_ratio'],'rebate',r['actual_rebate'],flush=True)
    valid=[r for r in rows if r.get('active_filled_makers') is not None and r.get('target_ratio') is not None]
    valid.sort(key=lambda r:r['active_filled_makers'])
    q=max(1,len(valid)//4)
    low=valid[:q]; high=valid[-q:]
    return {
        'trade_date':d,'candidate_conditions':len(cand),'tested_conditions':len(valid),
        'active_maker_min':min([r['active_filled_makers'] for r in valid],default=None),
        'active_maker_max':max([r['active_filled_makers'] for r in valid],default=None),
        'coefficient_median':med([r['target_ratio'] for r in valid]),
        'coefficient_min':min([r['target_ratio'] for r in valid],default=None),
        'coefficient_max':max([r['target_ratio'] for r in valid],default=None),
        'corr_log_active_makers_vs_coefficient':corr([math.log1p(r['active_filled_makers']) for r in valid],[r['target_ratio'] for r in valid]),
        'low_competition_median_coefficient':med([r['target_ratio'] for r in low]),
        'high_competition_median_coefficient':med([r['target_ratio'] for r in high]),
        'rows':rows,
    }


def leaderboard(category,limit=80):
    out=[]
    for off in range(0,limit,50):
        r=get('https://data-api.polymarket.com/v1/leaderboard',{'category':category,'timePeriod':'MONTH','orderBy':'VOL','limit':min(50,limit-off),'offset':off})
        if r.get('status')!=200:break
        rr=items(r.get('data'));out.extend(rr)
        if len(rr)<min(50,limit-off):break
    return out


def category_match(target,cid):
    gm=gamma_market(cid); cat=str(first(gm,'category') or '').lower(); title=str(first(gm,'question','title') or '').lower()
    info=clob_info(cid)
    if target=='sports': return bool(info.get('gst')) or 'sport' in cat
    if target=='finance': return 'finance' in cat or 'fed ' in title or 'interest rate' in title or 'treasury' in title
    return False


def category_closed_loop(target):
    lbcat='SPORTS' if target=='sports' else 'FINANCE'
    now=datetime.now(UTC)
    for ent in leaderboard(lbcat,100):
        w=str(first(ent,'proxyWallet','proxy_wallet') or '').lower()
        if not w:continue
        for d,activity_amt,paydate in payment_trade_dates(w,18)[:5]:
            rr,_=rebate_current(w,d)
            rr=sorted(rr,key=lambda x:fnum(first(x,'rebated_fees_usdc','earnings')),reverse=True)
            chosen=None
            for x in rr[:30]:
                cid=str(first(x,'condition_id','conditionId') or '').lower()
                if cid and category_match(target,cid):chosen=x;break
            if not chosen:continue
            cid=str(first(chosen,'condition_id','conditionId')).lower(); actual=fnum(first(chosen,'rebated_fees_usdc','earnings'))
            try:
                day=datetime.fromisoformat(d).replace(tzinfo=UTC); start=int(day.timestamp());end=start+86399
                from closed_loop_audit import maker_diff
                allr,tak,maker,missing=maker_diff(w,start,end)
            except Exception:continue
            maker=[t for t in maker if str(first(t,'conditionId','condition_id') or '').lower()==cid]
            if not maker:continue
            base=sum(fnum(first(t,'size'))*fnum(first(t,'price'))*(1-fnum(first(t,'price'))) for t in maker)
            if base<=0:continue
            ratio=actual/base
            info=clob_info(cid); fd=info.get('fd') if isinstance(info,dict) else None; fd_rate=fnum(fd.get('r')) if isinstance(fd,dict) else 0
            asset=str(first(maker[0],'asset') or '')
            result={
                'target':target,'wallet':w,'trade_date':d,'payment_date':paydate,'condition_id':cid,
                'title':first(gamma_market(cid),'question','title'),'gamma_category':first(gamma_market(cid),'category'),
                'all_user_rows':len(allr),'taker_user_rows':len(tak),'maker_user_rows_on_condition':len(maker),'taker_not_subset_missing':missing,
                'actual_rebate':actual,'reconstructed_base':base,'actual_over_base':ratio,
                'clob_fd':fd,'fd_rate':fd_rate,'inferred_rebate_share':ratio/fd_rate if fd_rate else None,
                'clob_tbf':info.get('tbf'),'clob_mbf':info.get('mbf'),'clob_gst':info.get('gst'),
                'asset':asset,'fee_rate_endpoint':fee_rate_endpoint(asset),
            }
            print('CATEGORY',target,w,d,'ratio',ratio,'fd',fd,'base_fee',result['fee_rate_endpoint'],flush=True)
            return result
    return {'target':target,'error':'no qualifying wallet-day found'}


def crypto_base_fee_example(mixed_result):
    for r in mixed_result.get('selected',[]):
        for v in r.get('validated_conditions',[]):
            if v.get('fd_rate') and abs(v['fd_rate']-.07)<.002:
                cid=v['condition_id']; info=clob_info(cid)
                # Resolve a token from CLOB market info; t[].t is token ID.
                toks=info.get('t') if isinstance(info,dict) else []
                asset=str(first(toks[0],'t','token_id') or '') if toks else ''
                return {'condition_id':cid,'title':v.get('title'),'fd':info.get('fd'),'tbf':info.get('tbf'),'mbf':info.get('mbf'),'asset':asset,'fee_rate_endpoint':fee_rate_endpoint(asset)}
    return {'error':'no crypto example'}


def main():
    started=datetime.now(UTC)
    mixed=mixed_wallet_day_audit(25)
    competition=competition_flatness_probe()
    sports=category_closed_loop('sports')
    finance=category_closed_loop('finance')
    crypto_fee=crypto_base_fee_example(mixed)
    out={
        'started_at':started.isoformat(),'finished_at':datetime.now(UTC).isoformat(),
        'mixed_wallet_days':mixed,
        'competition_flatness':competition,
        'category_closed_loops':{'crypto_fee_probe':crypto_fee,'sports':sports,'finance':finance},
    }
    with open('deep_empirical_audit.json','w') as f:json.dump(out,f,indent=2,default=str)
    # Compact CSV for the mixed subtraction sample.
    with open('mixed_wallet_days.csv','w',newline='') as f:
        cols=['wallet','trade_date','maker_share_of_user_notional','actual_rebate_total','all_user_notional','maker_notional','all_trade_rows','taker_rows','maker_rows','actual_rebate_covered_by_reconstructed_conditions']
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for r in mixed.get('selected',[]):w.writerow({k:r.get(k) for k in cols})
    print('=== DEEP EMPIRICAL AUDIT SUMMARY ===')
    print(json.dumps({
        'mixed_selected_n':mixed.get('selected_n'),'mixed_rebate_dollars':mixed.get('selected_actual_rebate_dollars'),
        'mixed_money_coverage':mixed.get('money_coverage_fraction'),'mixed_product_money_validation':mixed.get('validated_share_money_fraction'),
        'competition':{k:competition.get(k) for k in ['trade_date','tested_conditions','active_maker_min','active_maker_max','coefficient_median','coefficient_min','coefficient_max','corr_log_active_makers_vs_coefficient','low_competition_median_coefficient','high_competition_median_coefficient']},
        'sports':sports,'finance':finance,'crypto_fee_probe':crypto_fee,
    },indent=2,default=str),flush=True)

if __name__=='__main__':main()
