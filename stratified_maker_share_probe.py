#!/usr/bin/env python3
import csv, json, math, statistics
from collections import Counter
from datetime import datetime, timezone, timedelta
from closed_loop_audit import maker_diff, activity_all, fnum, first, ets

# 2 crypto-pure wallets from each original rebate-intensity decile (run #11), avoiding very high-volume extremes where possible.
SAMPLE=[
(1,'0xf3326d8fc30f7ed01bfade12c706cb52c397b1c0',0.0051088971907272,-0.0028845011379382,1.0,383510.986198),
(1,'0x673d4961db71c47fc4db14c41e42779a854b5234',0.0039788893089753,-0.0027362984770655,1.0,452047.986342),
(2,'0x428c7d482ee5ea01ebb4dc37f6811b259dc96ae4',0.0030639939550773,-0.0081194580883148,1.0,398174.055787),
(2,'0xa1f56cd95f1b71c8d0e09ca7060c8809408f224a',0.0028839438966155,-0.0074093792532631,0.997447086773026,457660.706073),
(3,'0xcd30457c790d8b35a08bcf3f894ad6bb52bc2dd0',0.0024274113951787,0.0045109552110768,0.9972687105860488,520028.620821),
(3,'0x0ab7993cd51f552a61e9211e424578f5bfc66fe6',0.0023314422584475,0.0036031436297186,1.0,1109447.206178),
(4,'0x7df1854c8bd10b573f1281c2e978b8104075491d',0.002165688673212,0.0581864730751086,1.0,506599.869857),
(4,'0x337b7fca244bf2ef3272469ee02a2bd541097e26',0.0018804854874403,0.0105396467857577,0.9965157734361316,549984.675185),
(5,'0x388537259dc9e693c1c9b96fdf07a63f6b7aca77',0.0010799700003701,0.0107383924367022,0.9993682523708725,632648.221493),
(5,'0x252d7bae5ec87553dcafb23e275c202ac0d3c456',0.0009013675986647,0.0292739298446569,1.0,1692422.716614),
(6,'0xe9ed54623043c0e5d6380270f07f0e044c884543',0.0005383725784889,-0.0045037024245123,1.0,474497.606689),
(6,'0x91aab8d0ae5c7dde7d9ea7ca49aeb985e09819b8',0.0004451229493413,0.0098892863109172,0.9818432539067651,1354867.909849),
(7,'0x44832d0d2ec11187c1e77d786feb15f6a50254c6',0.0002963816647383,0.0235024444607383,1.0,1541459.726948),
(7,'0x239e726f1f4131ca78a7e5abf1fec86bfc151c10',0.000239110343556,0.0155888750958323,1.0,859303.687763),
(8,'0xa5e9072668d0acda2455869f4244ddbb9edd74c6',0.0001557079949245,0.0098339541928689,0.9965430866035555,503090.416378),
(8,'0x13f0bcec1e2e60ec9acc3bee4d2da2fe9694a50f',0.0001290442287142,0.0085308965589454,1.0,635479.02),
(9,'0x2e31da14fb1ee55a1de99b1caebcbf7ed895d5fd',0.0000617345231652,-0.0164192055380169,0.9991724597108113,756741.894239),
(9,'0x67e38fc4bbf6b8929df424b1a95e7c3c4e392a16',0.0000448112991768,0.00808924018887,0.9942655367757022,518442.902276),
(10,'0x08327d9066e703fa718ec0ad78ff3873c1114146',0.0000122040677981,0.0402008972868283,0.9936028896155438,396834.898014),
(10,'0x239a14ee87dfcbd800e77499975aa23cb603a949',0.0000045004828178,0.001282608245812,0.9989816179055492,408333.966461),
]

def pearson(x,y):
    if len(x)<2:return None
    mx=sum(x)/len(x);my=sum(y)/len(y)
    num=sum((a-mx)*(b-my) for a,b in zip(x,y));dx=sum((a-mx)**2 for a in x);dy=sum((b-my)**2 for b in y)
    return num/math.sqrt(dx*dy) if dx>0 and dy>0 else None

def rankdata(vals):
    order=sorted(range(len(vals)),key=lambda i:vals[i]); ranks=[0]*len(vals);i=0
    while i<len(order):
        j=i+1
        while j<len(order) and vals[order[j]]==vals[order[i]]:j+=1
        r=(i+j-1)/2+1
        for k in range(i,j):ranks[order[k]]=r
        i=j
    return ranks

def spearman(x,y):return pearson(rankdata(x),rankdata(y))

# Use completed trading days Aug 1 through Aug 24. Rebate payments Aug 2..25 correspond to those trade dates.
start_dt=datetime(2026,8,1,tzinfo=timezone.utc); end_dt=datetime(2026,8,25,tzinfo=timezone.utc)-timedelta(seconds=1)
start=int(start_dt.timestamp());end=int(end_dt.timestamp())
pay_start=int((start_dt+timedelta(days=1)).timestamp());pay_end=int((datetime(2026,8,26,tzinfo=timezone.utc)-timedelta(seconds=1)).timestamp())
rows=[]
for dec,w,old_intensity,pnl_over_vol,crypto_share,lb_vol in SAMPLE:
    try:
        allr,takerr,maker,missing=maker_diff(w,start,end)
        all_notional=sum(fnum(first(t,'size'))*fnum(first(t,'price')) for t in allr)
        maker_notional=sum(fnum(first(t,'size'))*fnum(first(t,'price')) for t in maker)
        fee_curve_base=sum(fnum(first(t,'size'))*fnum(first(t,'price'))*(1-fnum(first(t,'price'))) for t in maker)
        maker_share=maker_notional/all_notional if all_notional else None
        tail_factor=fee_curve_base/maker_notional if maker_notional else None  # maker-notional-weighted E[1-p]
        evs,_=activity_all(w,pay_start,pay_end)
        rebate=sum(fnum(first(e,'usdcSize','amount','size')) for e in evs)
        data_intensity=rebate/all_notional if all_notional else None
        predicted_crypto_rebate=0.014*fee_curve_base
        pred_rel_err=(predicted_crypto_rebate-rebate)/rebate if rebate else None
        r={'decile':dec,'wallet':w,'old_rebate_over_lb_vol':old_intensity,'old_pnl_over_vol':pnl_over_vol,'crypto_share':crypto_share,'old_lb_vol':lb_vol,
           'all_rows':len(allr),'taker_rows':len(takerr),'maker_rows':len(maker),'taker_missing_from_all':missing,
           'all_notional_data':all_notional,'maker_notional_data':maker_notional,'true_maker_share_notional':maker_share,
           'maker_weighted_one_minus_p':tail_factor,'rebate_trade_days_aug1_24':rebate,'rebate_over_data_notional':data_intensity,
           'predicted_crypto_rebate_from_maker_fills':predicted_crypto_rebate,'predicted_vs_actual_rebate_rel_error':pred_rel_err,
           'data_notional_over_old_leaderboard_vol':all_notional/lb_vol if lb_vol else None}
        rows.append(r)
        print('ROW',dec,w,'share',maker_share,'tail',tail_factor,'reb',rebate,'pred_err',pred_rel_err,'rows',len(allr),len(takerr),flush=True)
    except Exception as e:
        rows.append({'decile':dec,'wallet':w,'error':repr(e)})
        print('ERROR',dec,w,repr(e),flush=True)
valid=[r for r in rows if not r.get('error') and r.get('true_maker_share_notional') is not None]
x_int=[r['old_rebate_over_lb_vol'] for r in valid];x_share=[r['true_maker_share_notional'] for r in valid];tail=[r['maker_weighted_one_minus_p'] or 0 for r in valid];y_abs=[abs(r['old_pnl_over_vol']) for r in valid]
summary={'window':'2026-08-01 through 2026-08-24 UTC completed trade days','n_valid':len(valid),'rows':rows,
 'correlations':{
   'pearson_old_intensity_vs_true_maker_share':pearson(x_int,x_share),'spearman_old_intensity_vs_true_maker_share':spearman(x_int,x_share),
   'pearson_true_maker_share_vs_abs_pnl_over_vol':pearson(x_share,y_abs),'spearman_true_maker_share_vs_abs_pnl_over_vol':spearman(x_share,y_abs),
   'pearson_old_intensity_vs_tail_factor':pearson(x_int,tail),'pearson_true_maker_share_vs_tail_factor':pearson(x_share,tail),
 },
 'prediction_error':{
   'median_abs_rel_error_crypto_formula':statistics.median([abs(r['predicted_vs_actual_rebate_rel_error']) for r in valid if r.get('predicted_vs_actual_rebate_rel_error') is not None]) if valid else None,
 }}
with open('stratified_maker_share_probe.json','w') as f:json.dump(summary,f,indent=2,default=str)
with open('stratified_maker_share_probe.csv','w',newline='') as f:
    keys=[]
    for r in rows:
        for k in r:
            if k not in keys:keys.append(k)
    w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)
print('=== STRATIFIED TRUE MAKER SHARE ===')
print(json.dumps({'n_valid':summary['n_valid'],'correlations':summary['correlations'],'prediction_error':summary['prediction_error'],'compact':[{k:r.get(k) for k in ('decile','wallet','true_maker_share_notional','maker_weighted_one_minus_p','old_rebate_over_lb_vol','old_pnl_over_vol','predicted_vs_actual_rebate_rel_error','error')} for r in rows]},indent=2,default=str))
