#!/usr/bin/env python3
import csv, json, math

PS=[0.01,0.02,0.05,0.10,0.15,0.25,0.50,0.75,0.85,0.90,0.95,0.98,0.99]
PRODUCT=0.014
rows=[]
for p in PS:
    rebate_per_share=PRODUCT*p*(1-p)
    rebate_per_notional=rebate_per_share/p
    outcome_sd_per_share=math.sqrt(p*(1-p))
    outcome_sd_per_dollar=outcome_sd_per_share/p
    rebate_per_outcome_sd=rebate_per_share/outcome_sd_per_share if outcome_sd_per_share else None
    rows.append({
        'p':p,
        'rebate_usdc_per_share':rebate_per_share,
        'rebate_cents_per_share':100*rebate_per_share,
        'rebate_per_dollar_notional':rebate_per_notional,
        'rebate_percent_of_notional':100*rebate_per_notional,
        'binary_outcome_sd_per_share':outcome_sd_per_share,
        'binary_outcome_sd_per_dollar_cost':outcome_sd_per_dollar,
        'rebate_per_unit_binary_outcome_sd':rebate_per_outcome_sd,
    })
with open('tail_unit_economics.csv','w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
with open('tail_unit_economics.json','w') as f:json.dump({'crypto_product':PRODUCT,'rows':rows},f,indent=2)
print(json.dumps({'p_0p50':next(r for r in rows if r['p']==.5),'p_0p05':next(r for r in rows if r['p']==.05)},indent=2))
print('RESULT_FILES=tail_unit_economics.csv,tail_unit_economics.json')
