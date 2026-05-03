"""
BTC/USDT 선물 퀀트 트레이딩 웹앱
pandas/numpy 없이 순수 Python으로 작성 (무료 서버 호환)
"""
from flask import Flask, jsonify, render_template
import requests
import json
import os
import math
from datetime import datetime

app = Flask(__name__)

SYMBOL    = "BTCUSDT"
TIMEFRAMES= ["15m","30m","1h","4h","1d"]
TF_WEIGHT = {"15m":1,"30m":1.5,"1h":2,"4h":3,"1d":4}
FAPI_BASE = "https://fapi.binance.com"
SIGNAL_LOG= "signal_log.json"
STATS_FILE= "win_stats.json"

def mean(lst):
    return sum(lst)/len(lst) if lst else 0

def stdev(lst):
    if len(lst)<2: return 0
    m=mean(lst)
    return math.sqrt(sum((x-m)**2 for x in lst)/(len(lst)-1))

def ema(prices, period):
    if not prices: return []
    k=2/(period+1)
    result=[prices[0]]
    for p in prices[1:]:
        result.append(p*k+result[-1]*(1-k))
    return result

def calc_rsi(closes, period=14):
    if len(closes)<period+1: return 50
    gains,losses=[],[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=mean(gains[-period:]); al=mean(losses[-period:])
    if al==0: return 100
    return 100-(100/(1+ag/al))

def calc_macd(closes, fast=12, slow=26, sig=9):
    if len(closes)<slow: return 0,0,0
    ef=ema(closes,fast); es=ema(closes,slow)
    macd=[ef[i]-es[i] for i in range(len(ef))]
    signal=ema(macd,sig)
    hist=macd[-1]-signal[-1]
    return macd[-1],signal[-1],hist

def calc_bb(closes, period=20):
    if len(closes)<period: return closes[-1],closes[-1],closes[-1]
    window=closes[-period:]
    mid=mean(window); sd=stdev(window)
    return mid+2*sd, mid, mid-2*sd

def calc_ema_val(closes, period):
    r=ema(closes,period)
    return r[-1] if r else closes[-1]

def vol_ratio(volumes):
    if len(volumes)<20: return 1
    ma=mean(volumes[-20:])
    return volumes[-1]/ma if ma>0 else 1

def fetch_futures_price():
    try:
        r=requests.get(f"{FAPI_BASE}/fapi/v1/ticker/price",
                       params={"symbol":SYMBOL},timeout=8)
        return float(r.json()["price"])
    except: return None

def fetch_klines(interval, limit=300):
    try:
        r=requests.get(f"{FAPI_BASE}/fapi/v1/klines",
                       params={"symbol":SYMBOL,"interval":interval,"limit":limit},
                       timeout=10)
        data=r.json()
        return {
            "closes":[float(d[4]) for d in data],
            "highs":[float(d[2]) for d in data],
            "lows":[float(d[3]) for d in data],
            "volumes":[float(d[5]) for d in data],
        }
    except: return None

def fetch_funding():
    try:
        r=requests.get(f"{FAPI_BASE}/fapi/v1/premiumIndex",
                       params={"symbol":SYMBOL},timeout=8)
        return float(r.json().get("lastFundingRate",0))*100
    except: return None

def fetch_oi():
    try:
        r=requests.get(f"{FAPI_BASE}/fapi/v1/openInterest",
                       params={"symbol":SYMBOL},timeout=8)
        return float(r.json().get("openInterest",0))
    except: return None

def analyze_tf(data):
    if not data or len(data["closes"])<50: return None
    closes=data["closes"]; vols=data["volumes"]
    price=closes[-1]
    rsi=calc_rsi(closes)
    macd_v,macd_sig,macd_hist=calc_macd(closes)
    prev_macd,prev_sig,_=calc_macd(closes[:-1]) if len(closes)>27 else (0,0,0)
    bb_u,bb_m,bb_l=calc_bb(closes)
    e20=calc_ema_val(closes,20); e60=calc_ema_val(closes,60)
    e120=calc_ema_val(closes,120); e240=calc_ema_val(closes,240)
    vr=vol_ratio(vols)
    cross_up=prev_macd<prev_sig and macd_v>=macd_sig
    cross_down=prev_macd>prev_sig and macd_v<=macd_sig
    bb_pos=(price-bb_l)/(bb_u-bb_l+1e-9)
    ls=ss=0; lr=[]; sr=[]

    if rsi<30:   ls+=3; lr.append(f"RSI 극과매도({rsi:.0f})")
    elif rsi<40: ls+=2; lr.append(f"RSI 과매도({rsi:.0f})")
    elif rsi<48: ls+=1; lr.append(f"RSI 저점({rsi:.0f})")
    if rsi>70:   ss+=3; sr.append(f"RSI 극과매수({rsi:.0f})")
    elif rsi>60: ss+=2; sr.append(f"RSI 과매수({rsi:.0f})")
    elif rsi>52: ss+=1; sr.append(f"RSI 고점({rsi:.0f})")

    if cross_up:      ls+=3; lr.append("MACD 골든크로스")
    elif macd_hist>0: ls+=1; lr.append("MACD 히스토 상승")
    if cross_down:    ss+=3; sr.append("MACD 데드크로스")
    elif macd_hist<0: ss+=1; sr.append("MACD 히스토 하락")

    if e20>e60>e120>e240:   ls+=3; lr.append("EMA 완전정배열")
    elif e20>e60>e120:      ls+=2; lr.append("EMA 정배열")
    elif e20>e60:           ls+=1; lr.append("EMA 단기정배열")
    if e20<e60<e120<e240:   ss+=3; sr.append("EMA 완전역배열")
    elif e20<e60<e120:      ss+=2; sr.append("EMA 역배열")
    elif e20<e60:           ss+=1; sr.append("EMA 단기역배열")
    if price>e120: ls+=1; lr.append("가격>EMA120")
    else:          ss+=1; sr.append("가격<EMA120")
    if price>e240: ls+=1; lr.append("가격>EMA240")
    else:          ss+=1; sr.append("가격<EMA240")

    if price<=bb_l:   ls+=3; lr.append("BB 하단 돌파")
    elif bb_pos<0.25: ls+=1; lr.append("BB 하단 근접")
    if price>=bb_u:   ss+=3; sr.append("BB 상단 돌파")
    elif bb_pos>0.75: ss+=1; sr.append("BB 상단 근접")

    if vr>=2.0:
        if ls>=ss: ls+=2; lr.append(f"거래량 폭증(x{vr:.1f})")
        else:      ss+=2; sr.append(f"거래량 폭증(x{vr:.1f})")
    elif vr>=1.5:
        if ls>=ss: ls+=1; lr.append(f"거래량 증가(x{vr:.1f})")
        else:      ss+=1; sr.append(f"거래량 증가(x{vr:.1f})")

    total=ls+ss
    if total==0: d="NEUTRAL"; conf=0
    elif ls>ss:  d="LONG";    conf=round(ls/total*100)
    elif ss>ls:  d="SHORT";   conf=round(ss/total*100)
    else:        d="NEUTRAL"; conf=50

    risk=10
    if d=="LONG":
        if rsi<40: risk-=2
        if e20>e60>e120: risk-=2
        if cross_up: risk-=1
        if bb_pos<0.3: risk-=1
        if vr>=1.5: risk-=1
    elif d=="SHORT":
        if rsi>60: risk-=2
        if e20<e60<e120: risk-=2
        if cross_down: risk-=1
        if bb_pos>0.7: risk-=1
        if vr>=1.5: risk-=1
    risk=max(0,min(10,risk))

    return {
        "direction":d,"confidence":conf,
        "score_long":ls,"score_short":ss,
        "reasons_long":lr,"reasons_short":sr,
        "risk":risk,"rsi":round(rsi,1),
        "macd_hist":round(macd_hist,2),
        "ema20":round(e20,1),"ema60":round(e60,1),
        "ema120":round(e120,1),"ema240":round(e240,1),
        "bb_u":round(bb_u,1),"bb_m":round(bb_m,1),"bb_l":round(bb_l,1),
        "vol_r":round(vr,2),"price":price,
    }

def calc_leverage(conf, risk):
    base=conf/100
    if risk<=2:   lev=int(base*20)
    elif risk<=4: lev=int(base*15)
    elif risk<=6: lev=int(base*10)
    elif risk<=8: lev=int(base*7)
    else:         lev=int(base*5)
    return max(3,min(20,lev))

def combine(tf_results):
    wl=ws=tw=0; risks=[]
    for tf,res in tf_results.items():
        if not res: continue
        w=TF_WEIGHT[tf]; tw+=w
        if res["direction"]=="LONG":    wl+=w*res["confidence"]
        elif res["direction"]=="SHORT": ws+=w*res["confidence"]
        else: wl+=w*50; ws+=w*50
        risks.append(res["risk"])
    if tw==0: return None
    sl=wl/tw; ss=ws/tw
    if abs(sl-ss)<5: d="NEUTRAL"; c=50
    elif sl>ss:      d="LONG";    c=round(sl)
    else:            d="SHORT";   c=round(ss)
    mr=round(sum(risks)/len(risks),1) if risks else 5
    dirs=[r["direction"] for r in tf_results.values() if r]
    return {
        "direction":d,"confidence":c,"risk":mr,
        "leverage":calc_leverage(c,mr),
        "agree_long":dirs.count("LONG"),
        "agree_short":dirs.count("SHORT"),
        "agree_neutral":dirs.count("NEUTRAL"),
        "score_l":round(sl,1),"score_s":round(ss,1),
    }

def backtest_tf(data, tf):
    if not data or len(data["closes"])<60: return []
    closes=data["closes"]; highs=data["highs"]; lows=data["lows"]
    tp_map={"15m":(0.012,0.007),"30m":(0.018,0.010),
            "1h":(0.025,0.013),"4h":(0.040,0.020),"1d":(0.070,0.035)}
    tp_r,sl_r=tp_map.get(tf,(0.015,0.008))
    results=[]
    for i in range(50,len(closes)-10):
        sub=closes[:i+1]
        rsi=calc_rsi(sub)
        mv,ms,_=calc_macd(sub)
        pv,ps,_=calc_macd(sub[:-1]) if len(sub)>27 else (0,0,0)
        e20=calc_ema_val(sub,20); e60=calc_ema_val(sub,60); e120=calc_ema_val(sub,120)
        cu=pv<ps and mv>=ms; cd=pv>ps and mv<=ms
        if cu and e20>e60>e120 and rsi<45:   direction="LONG"
        elif cd and e20<e60<e120 and rsi>55: direction="SHORT"
        else: continue
        price=closes[i]
        tp_p=price*(1+tp_r) if direction=="LONG" else price*(1-tp_r)
        sl_p=price*(1-sl_r) if direction=="LONG" else price*(1+sl_r)
        win=None
        for j in range(i+1,min(i+21,len(closes))):
            if direction=="LONG":
                if highs[j]>=tp_p: win=True; break
                if lows[j]<=sl_p:  win=False; break
            else:
                if lows[j]<=tp_p:  win=True; break
                if highs[j]>=sl_p: win=False; break
        if win is None: win=False
        results.append({"direction":direction,"win":win})
    return results

def init_backtest():
    stats={}
    for tf in TIMEFRAMES:
        data=fetch_klines(tf,limit=300)
        res=backtest_tf(data,tf)
        if not res: continue
        wins=sum(1 for x in res if x["win"]); total=len(res)
        wr=round(wins/total*100,1) if total else 50.0
        lt=sum(1 for x in res if x["direction"]=="LONG")
        lw=sum(1 for x in res if x["direction"]=="LONG" and x["win"])
        st2=sum(1 for x in res if x["direction"]=="SHORT")
        sw=sum(1 for x in res if x["direction"]=="SHORT" and x["win"])
        stats[tf]={"total":total,"wins":wins,"win_rate":wr,
                   "long_total":lt,"long_wins":lw,"short_total":st2,"short_wins":sw}
    with open(STATS_FILE,"w") as f: json.dump(stats,f)
    return stats

def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            try: return json.load(f)
            except: return {}
    return {}

def save_stats(s):
    with open(STATS_FILE,"w") as f: json.dump(s,f)

def resolve_signals(price, stats):
    if not os.path.exists(SIGNAL_LOG): return stats
    with open(SIGNAL_LOG) as f:
        try: log=json.load(f)
        except: return stats
    changed=False
    for e in log:
        if e["resolved"]: continue
        ep=e["price"]; d=e["direction"]
        tp=ep*1.020 if d=="LONG" else ep*0.980
        sl=ep*0.988 if d=="LONG" else ep*1.012
        age=(datetime.now()-datetime.fromisoformat(e["time"])).total_seconds()/3600
        win=None
        if d=="LONG":
            if price>=tp: win=True
            elif price<=sl: win=False
        elif d=="SHORT":
            if price<=tp: win=True
            elif price>=sl: win=False
        if win is None and age>48: win=False
        if win is not None:
            e["resolved"]=True; e["win"]=win; changed=True
            tk="1h"
            if tk not in stats:
                stats[tk]={"total":0,"wins":0,"win_rate":50.0,
                           "long_total":0,"long_wins":0,"short_total":0,"short_wins":0}
            s=stats[tk]; s["total"]+=1
            if win: s["wins"]+=1
            if d=="LONG":
                s["long_total"]+=1
                if win: s["long_wins"]+=1
            else:
                s["short_total"]+=1
                if win: s["short_wins"]+=1
            s["win_rate"]=round(s["wins"]/s["total"]*100,1)
    if changed:
        with open(SIGNAL_LOG,"w") as f: json.dump(log,f)
        save_stats(stats)
    return stats

def log_signal(combined, price):
    log=[]
    if os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG) as f:
            try: log=json.load(f)
            except: log=[]
    e={"id":len(log)+1,"time":datetime.now().isoformat(),
       "direction":combined["direction"],"confidence":combined["confidence"],
       "price":price,"leverage":combined["leverage"],
       "resolved":False,"win":None}
    log.append(e)
    with open(SIGNAL_LOG,"w") as f: json.dump(log,f)
    return e["id"]

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/signal")
def api_signal():
    price=fetch_futures_price()
    if not price: return jsonify({"error":"가격 수집 실패"}),503
    stats=load_stats()
    if not stats: stats=init_backtest()
    stats=resolve_signals(price,stats)
    tf_results={}
    for tf in TIMEFRAMES:
        data=fetch_klines(tf)
        tf_results[tf]=analyze_tf(data)
    combined=combine(tf_results)
    if not combined: return jsonify({"error":"분석 실패"}),503
    funding=fetch_funding(); oi=fetch_oi()
    tp_r,sl_r=0.025,0.013
    if combined["direction"]=="LONG":
        tp=round(price*(1+tp_r),2); sl=round(price*(1-sl_r),2)
    elif combined["direction"]=="SHORT":
        tp=round(price*(1-tp_r),2); sl=round(price*(1+sl_r),2)
    else: tp=sl=None
    log=[]
    if os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG) as f:
            try: log=json.load(f)
            except: log=[]
    prev_dir=log[-1]["direction"] if log else None
    sig_id=None
    if combined["direction"]!="NEUTRAL" and combined["direction"]!=prev_dir:
        sig_id=log_signal(combined,price)
    stats_summary={}
    for tf in TIMEFRAMES:
        s=stats.get(tf,{})
        if s:
            lt=s.get("long_total",0); lw=s.get("long_wins",0)
            st2=s.get("short_total",0); sw=s.get("short_wins",0)
            stats_summary[tf]={
                "win_rate":s.get("win_rate",0),"total":s.get("total",0),
                "long_wr":round(lw/lt*100,1) if lt>0 else 0,
                "short_wr":round(sw/st2*100,1) if st2>0 else 0,
            }
    return jsonify({
        "price":price,"funding":funding,"oi":oi,
        "combined":combined,
        "tf_results":{tf:(r if r else {}) for tf,r in tf_results.items()},
        "tp":tp,"sl":sl,"tp_pct":tp_r*100,"sl_pct":sl_r*100,
        "rr":round(tp_r/sl_r,1),"stats":stats_summary,
        "signal_id":sig_id,
        "time":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route("/api/history")
def api_history():
    if not os.path.exists(SIGNAL_LOG): return jsonify([])
    with open(SIGNAL_LOG) as f:
        try: log=json.load(f)
        except: log=[]
    return jsonify(log[-20:])

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)
