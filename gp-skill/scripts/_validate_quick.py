"""Quick validation: print key fundamentals for benchmark stocks."""
import sys
sys.path.insert(0, ".")
from scripts.signals import analyze_symbol

SYMBOLS = ["600519", "300750", "002594", "000858"]

for sym in SYMBOLS:
    try:
        card = analyze_symbol(sym)
        d = card.to_dict()
        f = d.get("fundamentals", {})
        total_mcap = f.get("total_mcap")
        float_mcap = f.get("float_mcap")
        mcap_str = ""
        if total_mcap:
            mcap_str = f"总市值:{float(total_mcap)/1e8:.0f}亿"
        if float_mcap:
            mcap_str += f"  流通:{float(float_mcap)/1e8:.0f}亿"
        mf = f.get("moneyflow")
        mf_str = ""
        if mf:
            mf_str = f"  主力净流入:{mf.get('main_net_total',0)/10000:.2f}亿({mf.get('days')}日)"
        fc = f.get("forecast")
        fc_str = ""
        if fc:
            fc_str = f"  预告:{fc.get('type')} {fc.get('p_change')}%"
        print(f"=== {sym} {d['name']} ===")
        print(f"  收盘:{d['latest_close']}  日期:{d['latest_date']}")
        print(f"  PE:{f.get('pe_ttm')}  PB:{f.get('pb')}  {mcap_str}")
        print(f"  ROE:{f.get('roe_ttm')}  净利率:{f.get('net_margin')}  毛利率:{f.get('gross_margin')}")
        print(f"  资产负债率:{f.get('debt_ratio')}")
        print(f"  营收同比:{f.get('revenue_growth_yoy')}  净利同比:{f.get('earnings_growth_yoy')}")
        print(f"  技术:{d['technical_score']}  基本面:{d['fundamental_score']}  综合:{d['combined_score']}  动作:{d['action']}")
        print(f"  {mf_str}{fc_str}")
    except Exception as e:
        print(f"{sym} ERROR: {e}")
    print()
