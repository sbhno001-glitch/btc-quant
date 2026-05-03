"""
BTC 선물 퀀트 트레이딩 웹앱 - Flask 백엔드
"""
from flask import Flask, jsonify, render_template
import requests
import pandas as pd
import numpy as np
import json
import os
from datetime import datetime

app = Flask(__name__)

SYMBOL     = "BTCUSDT"
TIMEFRAMES = ["15m","30m","1h","4h","1d"]
TF_WEIGHT  = {"15m":1,"30m":1.5,"1h":2,"4h":3,"1d":4}
TF_LABEL   = {"15m":"15분","30m":"30분","1h":"1시간","4h":"4시간","1d":"1일"}
FAPI_BASE  = "https://fapi.binance.com"
STATS_FILE = "win_stats.json"
SIGNAL_LOG = "signal_log.json"

# ── 데이터 수집 ───────────────────────────────
def fetch_futures_price():
    try:
        r = requests.get(f"{FAPI_BASE}/fapi/v1/ticker/price",
                         params={"symbol":SYMBOL}, timeout=8)
        return float(r.json()["price"])
    except: return None

def fetch_klines(interval, limit=300):
    try:
        r = requests.get(f"{FAPI_BASE}/fapi/v1/klines",
                         params={"symbol":SYMBOL,"interval":interval,"limit":limit},
                         timeout=10)
        data = r.json()
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","qav","trades","tbbav","tbqav","ignore"])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df
    except: return None

def fetch_funding_rate():
    try:
        r = requests.get(f"{FAPI_BASE}/fapi/v1/premiumIndex",
                         params={"symbol":SYMBOL}, timeout=8)
        return float(r.json().get("lastFundingRate",0))*100
    except: return None

def fetch_open_interest():
    try:
        r = requests.get(f"{FAPI_BASE}/fapi/v1/openInterest",
                         params={"symbol":SYMBOL}, timeout=8)
        return float(r.json().get("openInterest",0))
    except: return None

# ── 지표 계산 ─────────────────────────────────
def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100-(100/(1+g/l.replace(0,np.nan)))

def calc_macd(s, fast=12, slow=26, sig=9):
    ef=s.ewm(span=fast,adjust=False).mean()
    es=s.ewm(span=slow,adjust=False).mean()
    m=ef-es; sg=m.ewm(span=sig,adjust=False).mean()
    return m, sg, m-sg

def calc_bb(s, p=20, mult=2):
    mid=s.rolling(p).mean(); std=s.rolling(p).std()
    return mid+mult*std, mid, mid-mult*std

def add_indicators(df):
    c=df["close"]; v=df["volume"]
    for p in [20,60,120,240]:
        df[f"ema{p}"]=c.ewm(span=p,adjust=False).mean()
    df["rsi"]=calc_rsi(c)
    df["macd"],df["macd_sig"],df["macd_hist"]=calc_macd(c)
    df["bb_u"],df["bb_m"],df["bb_l"]=calc_bb(c)
    tr=pd.concat([df["high"]-df["low"],
                  (df["high"]-c.shift()).abs(),
                  (df["low"]-c.shift()).abs()],axis=1).max(axis=1)
    df["atr"]=tr.rolling(14).mean()
    df["vol_ma"]=v.rolling(20).mean()
    df["vol_r"]=v/df["vol_ma"]
    rsi=df["rsi"]
    df["stoch_rsi"]=(rsi-rsi.rolling(14).min())/(rsi.rolling(14).max()-rsi.rolling(14).min()+1e-9)
    return df

# ── 단일 TF 분석 ──────────────────────────────
def analyze_tf(df):
    if df is None or len(df)<250: return None
    df=add_indicators(df)
    r=df.iloc[-1]; p=df.iloc[-2]
    price=r["close"]
    ls=ss=0; lr=[]; sr=[]

    rsi=r["rsi"]
    if rsi<30:   ls+=3; lr.append(f"RSI 극과매도({rsi:.0f})")
    elif rsi<40: ls+=2; lr.append(f"RSI 과매도({rsi:.0f})")
    elif rsi<48: ls+=1; lr.append(f"RSI 저점({rsi:.0f})")
    if rsi>70:   ss+=3; sr.append(f"RSI 극과매수({rsi:.0f})")
    elif rsi>60: ss+=2; sr.append(f"RSI 과매수({rsi:.0f})")
    elif rsi>52: ss+=1; sr.append(f"RSI 고점({rsi:.0f})")

    st=r["stoch_rsi"]
    if st<0.2: ls+=1; lr.append(f"StochRSI 과매도({st:.2f})")
    if st>0.8: ss+=1; sr.append(f"StochRSI 과매수({st:.2f})")

    cu=p["macd"]<p["macd_sig"] and r["macd"]>=r["macd_sig"]
    cd=p["macd"]>p["macd_sig"] and r["macd"]<=r["macd_sig"]
    if cu:                                        ls+=3; lr.append("MACD 골든크로스")
    elif r["macd_hist"]>0 and r["macd_hist"]>p["macd_hist"]: ls+=1; lr.append("MACD 히스토 상승")
    if cd:                                        ss+=3; sr.append("MACD 데드크로스")
    elif r["macd_hist"]<0 and r["macd_hist"]<p["macd_hist"]: ss+=1; sr.append("MACD 히스토 하락")

    e20,e60,e120,e240=r["ema20"],r["ema60"],r["ema120"],r["ema240"]
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

    bb_pos=(price-r["bb_l"])/(r["bb_u"]-r["bb_l"]+1e-9)
    if price<=r["bb_l"]:    ls+=3; lr.append("BB 하단 돌파")
    elif bb_pos<0.25:       ls+=1; lr.append("BB 하단 근접")
    if price>=r["bb_u"]:    ss+=3; sr.append("BB 상단 돌파")
    elif bb_pos>0.75:       ss+=1; sr.append("BB 상단 근접")

    vr=r["vol_r"]
    if vr>=2.0:
        if ls>=ss: ls+=2; lr.append(f"거래량 폭증(×{vr:.1f})")
        else:      ss+=2; sr.append(f"거래량 폭증(×{vr:.1f})")
    elif vr>=1.5:
        if ls>=ss: ls+=1; lr.append(f"거래량 증가(×{vr:.1f})")
        else:      ss+=1; sr.append(f"거래량 증가(×{vr:.1f})")

    total=ls+ss
    if total==0: direction="NEUTRAL"; conf=0
    elif ls>ss:  direction="LONG";    conf=round(ls/total*100)
    elif ss>ls:  direction="SHORT";   conf=round(ss/total*100)
    else:        direction="NEUTRAL"; conf=50

    risk=10
    if direction=="LONG":
        if rsi<40: risk-=2
        if e20>e60>e120: risk-=2
        if cu: risk-=1
        if bb_pos<0.3: risk-=1
        if vr>=1.5: risk-=1
    elif direction=="SHORT":
        if rsi>60: risk-=2
        if e20<e60<e120: risk-=2
        if cd: risk-=1
        if bb_pos>0.7: risk-=1
        if vr>=1.5: risk-=1
    risk=max(0,min(10,risk))

    return {
        "direction":direction,"confidence":conf,
        "score_long":ls,"score_short":ss,
        "reasons_long":lr,"reasons_short":sr,
        "risk":risk,"rsi":round(rsi,1),
        "stoch_rsi":round(float(st),3),
        "macd_hist":round(float(r["macd_hist"]),2),
        "ema20":round(e20,1),"ema60":round(e60,1),
        "ema120":round(e120,1),"ema240":round(e240,1),
        "bb_u":round(float(r["bb_u"]),1),
        "bb_m":round(float(r["bb_m"]),1),
        "bb_l":round(float(r["bb_l"]),1),
        "atr":round(float(r["atr"]),1),
        "vol_r":round(float(vr),2),"price":price,
    }

# ── 종합 판단 ─────────────────────────────────
def calc_leverage(confidence, risk_score):
    base=confidence/100
    if risk_score<=2:   lev=int(base*20)
    elif risk_score<=4: lev=int(base*15)
    elif risk_score<=6: lev=int(base*10)
    elif risk_score<=8: lev=int(base*7)
    else:               lev=int(base*5)
    return max(3,min(20,lev))

def combine_signals(tf_results):
    wl=ws=tw=0; risks=[]; confs=[]
    for tf,res in tf_results.items():
        if res is None: continue
        w=TF_WEIGHT[tf]; tw+=w
        if res["direction"]=="LONG":    wl+=w*res["confidence"]
        elif res["direction"]=="SHORT": ws+=w*res["confidence"]
        else: wl+=w*50; ws+=w*50
        risks.append(res["risk"]); confs.append(res["confidence"])
    if tw==0: return None
    sl=wl/tw; ss=ws/tw
    if abs(sl-ss)<5: d="NEUTRAL"; c=50
    elif sl>ss:      d="LONG";    c=round(sl)
    else:            d="SHORT";   c=round(ss)
    mr=round(float(np.mean(risks)),1) if risks else 5
    dirs=[r["direction"] for r in tf_results.values() if r]
    return {
        "direction":d,"confidence":c,"risk":mr,
        "leverage":calc_leverage(c,mr),
        "agree_long":dirs.count("LONG"),
        "agree_short":dirs.count("SHORT"),
        "agree_neutral":dirs.count("NEUTRAL"),
        "score_l":round(sl,1),"score_s":round(ss,1),
    }

# ── 승률 통계 ─────────────────────────────────
def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            try: return json.load(f)
            except: return {}
    return {}

def save_stats(stats):
    with open(STATS_FILE,"w") as f:
        json.dump(stats,f,indent=2)

def backtest_tf(df, tf):
    if df is None or len(df)<260: return []
    df=add_indicators(df).dropna().reset_index(drop=True)
    tp_map={"15m":(0.012,0.007),"30m":(0.018,0.010),
            "1h":(0.025,0.013),"4h":(0.040,0.020),"1d":(0.070,0.035)}
    tp_r,sl_r=tp_map.get(tf,(0.015,0.008))
    results=[]
    for i in range(len(df)-30):
        sub=df.iloc[:i+1]; r=sub.iloc[-1]; p=sub.iloc[-2]
        price=r["close"]
        cu=p["macd"]<p["macd_sig"] and r["macd"]>=r["macd_sig"]
        cd=p["macd"]>p["macd_sig"] and r["macd"]<=r["macd_sig"]
        eb=r["ema20"]>r["ema60"]>r["ema120"]
        es=r["ema20"]<r["ema60"]<r["ema120"]
        if cu and eb and r["rsi"]<45:   direction="LONG"
        elif cd and es and r["rsi"]>55: direction="SHORT"
        else: continue
        future=df.iloc[i+1:i+31]
        tp_p=price*(1+tp_r) if direction=="LONG" else price*(1-tp_r)
        sl_p=price*(1-sl_r) if direction=="LONG" else price*(1+sl_r)
        win=None
        for _,fr in future.iterrows():
            if direction=="LONG":
                if fr["high"]>=tp_p: win=True; break
                if fr["low"]<=sl_p:  win=False; break
            else:
                if fr["low"]<=tp_p:  win=True; break
                if fr["high"]>=sl_p: win=False; break
        if win is None: win=False
        results.append({"tf":tf,"direction":direction,"win":win})
    return results

def init_backtest():
    stats={}
    for tf in TIMEFRAMES:
        df=fetch_klines(tf,limit=500)
        res=backtest_tf(df,tf)
        if not res: continue
        wins=sum(1 for x in res if x["win"]); total=len(res)
        wr=round(wins/total*100,1) if total else 50.0
        stats[tf]={
            "total":total,"wins":wins,"win_rate":wr,
            "long_total":sum(1 for x in res if x["direction"]=="LONG"),
            "long_wins":sum(1 for x in res if x["direction"]=="LONG" and x["win"]),
            "short_total":sum(1 for x in res if x["direction"]=="SHORT"),
            "short_wins":sum(1 for x in res if x["direction"]=="SHORT" and x["win"]),
        }
    save_stats(stats)
    return stats

def resolve_signals(current_price, stats):
    if not os.path.exists(SIGNAL_LOG): return stats
    with open(SIGNAL_LOG) as f:
        try: log=json.load(f)
        except: return stats
    changed=False
    for entry in log:
        if entry["resolved"]: continue
        ep=entry["price"]; d=entry["direction"]
        tp_p=ep*1.020 if d=="LONG" else ep*0.980
        sl_p=ep*0.988 if d=="LONG" else ep*1.012
        age=(datetime.now()-datetime.fromisoformat(entry["time"])).total_seconds()/3600
        win=None
        if d=="LONG":
            if current_price>=tp_p: win=True
            elif current_price<=sl_p: win=False
        elif d=="SHORT":
            if current_price<=tp_p: win=True
            elif current_price>=sl_p: win=False
        if win is None and age>48: win=False
        if win is not None:
            entry["resolved"]=True; entry["win"]=win; changed=True
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
            s["win_rate"]=round(s["wins"]/s["total"]*100,1) if s["total"] else 50.0
    if changed:
        with open(SIGNAL_LOG,"w") as f: json.dump(log,f,indent=2)
        save_stats(stats)
    return stats

def log_signal(combined, price):
    log=[]
    if os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG) as f:
            try: log=json.load(f)
            except: log=[]
    entry={
        "id":len(log)+1,"time":datetime.now().isoformat(),
        "direction":combined["direction"],"confidence":combined["confidence"],
        "price":price,"leverage":combined["leverage"],
        "resolved":False,"win":None,
    }
    log.append(entry)
    with open(SIGNAL_LOG,"w") as f: json.dump(log,f,indent=2)
    return entry["id"]

# ── API 엔드포인트 ────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/signal")
def api_signal():
    price=fetch_futures_price()
    if price is None:
        return jsonify({"error":"가격 수집 실패"}), 503

    stats=load_stats()
    if not stats:
        stats=init_backtest()

    stats=resolve_signals(price,stats)

    tf_results={}
    for tf in TIMEFRAMES:
        df=fetch_klines(tf)
        tf_results[tf]=analyze_tf(df)

    combined=combine_signals(tf_results)
    if combined is None:
        return jsonify({"error":"분석 실패"}), 503

    funding=fetch_funding_rate()
    oi=fetch_open_interest()

    # TP/SL 계산
    tp_r,sl_r=0.025,0.013
    if combined["direction"]=="LONG":
        tp=round(price*(1+tp_r),2); sl=round(price*(1-sl_r),2)
    elif combined["direction"]=="SHORT":
        tp=round(price*(1-tp_r),2); sl=round(price*(1+sl_r),2)
    else:
        tp=sl=None

    # 최근 신호 기록
    prev_log=[]
    if os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG) as f:
            try: prev_log=json.load(f)
            except: prev_log=[]
    prev_dir=prev_log[-1]["direction"] if prev_log else None
    sig_id=None
    if combined["direction"]!="NEUTRAL" and combined["direction"]!=prev_dir:
        sig_id=log_signal(combined,price)

    # 통계 요약
    stats_summary={}
    for tf in TIMEFRAMES:
        s=stats.get(tf,{})
        if s:
            lt=s.get("long_total",0); lw=s.get("long_wins",0)
            st2=s.get("short_total",0); sw=s.get("short_wins",0)
            stats_summary[tf]={
                "win_rate":s.get("win_rate",0),
                "total":s.get("total",0),
                "long_wr":round(lw/lt*100,1) if lt>0 else 0,
                "short_wr":round(sw/st2*100,1) if st2>0 else 0,
            }

    return jsonify({
        "price":price,"funding":funding,"oi":oi,
        "combined":combined,
        "tf_results":{tf:(r if r else {}) for tf,r in tf_results.items()},
        "tp":tp,"sl":sl,"tp_pct":tp_r*100,"sl_pct":sl_r*100,
        "rr":round(tp_r/sl_r,1),
        "stats":stats_summary,
        "signal_id":sig_id,
        "time":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route("/api/history")
def api_history():
    if not os.path.exists(SIGNAL_LOG):
        return jsonify([])
    with open(SIGNAL_LOG) as f:
        try: log=json.load(f)
        except: log=[]
    return jsonify(log[-20:])

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)
