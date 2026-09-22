#!/usr/bin/env python3
import json, os, pathlib, subprocess, urllib.parse, urllib.request

ROOT=pathlib.Path("/home/apr/eth-hedge-live")
ROOT.mkdir(parents=True,exist_ok=True)
CFG=ROOT/"config.json"
BOT=ROOT/"monitor_bot.py"
ACCOUNT=749122
KEY_INDEX=45

url="https://mainnet.zklighter.elliot.ai/api/v1/account?"+urllib.parse.urlencode({
    "by":"index","value":str(ACCOUNT),"active_only":"false"
})
req=urllib.request.Request(url,headers={"User-Agent":"eth-hedge-deploy/1.0"})
with urllib.request.urlopen(req,timeout=15) as r:
    j=json.load(r)
a=(j.get("accounts") or [{}])[0]
pos=[]
for p in a.get("positions") or []:
    try:q=float(p.get("position") or 0)
    except Exception:q=0.0
    if abs(q)>1e-12: pos.append({"symbol":p.get("symbol"),"market_id":p.get("market_id"),"position":q})
if pos:
    raise SystemExit("Refusing deploy: dedicated account is not flat")

cfg={
    "account_index":749122,
    "api_key_index":45,
    "mode":"LOCKED_NO_ORDERS",
    "max_short_eth":0.1,
    "signal_lookback_bars":6,
    "signal_rise_threshold":0.012,
    "signal_taker_buy_share_max":0.50,
    "tp_pct":0.003,
    "adverse_alert_pct":0.015,
    "hold_alert_minutes":60,
    "cooldown_minutes":60,
    "spot_actions_allowed":False,
    "auto_stop_loss":False,
    "auto_timeout_exit":False
}
CFG.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n")
os.chmod(CFG,0o600)
BOT.write_text("#!/usr/bin/env python3\nfrom __future__ import annotations\nimport argparse, fcntl, json, os, pathlib, time, urllib.parse, urllib.request\nfrom datetime import datetime, timezone\n\nROOT=pathlib.Path(\"/home/apr/eth-hedge-live\")\nCONFIG=ROOT/\"config.json\"\nSTATE=ROOT/\"state.json\"\nLEDGER=ROOT/\"ledger.jsonl\"\nLOG=ROOT/\"monitor.log\"\nLOCK=ROOT/\"run.lock\"\n\nACCOUNT_INDEX=749122\nAPI_KEY_INDEX=45\nBINANCE_API=\"https://fapi.binance.com/fapi/v1/klines\"\n\ndef now_ms(): return int(time.time()*1000)\ndef iso(ms=None):\n    if ms is None: return datetime.now(timezone.utc).isoformat()\n    return datetime.fromtimestamp(ms/1000,tz=timezone.utc).isoformat()\n\ndef emit(event, **data):\n    row={\"event\":event,\"at\":iso(),**data}\n    with LEDGER.open(\"a\",encoding=\"utf-8\") as f:\n        f.write(json.dumps(row,ensure_ascii=False,separators=(\",\",\":\"))+\"\\n\")\n    with LOG.open(\"a\",encoding=\"utf-8\") as f:\n        f.write(json.dumps(row,ensure_ascii=False,separators=(\",\",\":\"))+\"\\n\")\n\ndef load_cfg():\n    return json.loads(CONFIG.read_text())\n\ndef load_state():\n    if not STATE.exists():\n        return {\n            \"version\":1,\n            \"last_processed_close_ts\":0,\n            \"cooldown_until_ts\":0,\n            \"active_signal\":None,\n            \"stats\":{\"signals\":0,\"alerts_1_5\":0,\"alerts_60m\":0},\n        }\n    return json.loads(STATE.read_text())\n\ndef save_state(s):\n    tmp=STATE.with_suffix(\".tmp\")\n    tmp.write_text(json.dumps(s,ensure_ascii=False,indent=2)+\"\\n\")\n    os.replace(tmp,STATE)\n\ndef account():\n    url=\"https://mainnet.zklighter.elliot.ai/api/v1/account?\"+urllib.parse.urlencode({\n        \"by\":\"index\",\"value\":str(ACCOUNT_INDEX),\"active_only\":\"false\"\n    })\n    req=urllib.request.Request(url,headers={\"User-Agent\":\"eth-hedge-monitor/1.0\"})\n    with urllib.request.urlopen(req,timeout=15) as r:\n        j=json.load(r)\n    a=(j.get(\"accounts\") or [{}])[0]\n    positions=[]\n    for p in a.get(\"positions\") or []:\n        try:q=float(p.get(\"position\") or 0)\n        except Exception:q=0.0\n        if abs(q)<=1e-12: continue\n        positions.append({\n            \"symbol\":p.get(\"symbol\"),\n            \"market_id\":p.get(\"market_id\"),\n            \"sign\":p.get(\"sign\"),\n            \"position\":q,\n            \"avg_entry_price\":p.get(\"avg_entry_price\"),\n            \"liquidation_price\":p.get(\"liquidation_price\"),\n            \"unrealized_pnl\":p.get(\"unrealized_pnl\"),\n        })\n    return {\n        \"available_balance\":a.get(\"available_balance\"),\n        \"collateral\":a.get(\"collateral\"),\n        \"positions\":positions,\n    }\n\ndef bars():\n    qs=urllib.parse.urlencode({\"symbol\":\"ETHUSDT\",\"interval\":\"5m\",\"limit\":200})\n    req=urllib.request.Request(BINANCE_API+\"?\"+qs,headers={\"User-Agent\":\"eth-hedge-monitor/1.0\"})\n    with urllib.request.urlopen(req,timeout=20) as r:\n        raw=json.load(r)\n    cutoff=now_ms()-1000\n    out=[]\n    for x in raw:\n        b={\n            \"open_ts\":int(x[0]),\"open\":float(x[1]),\"high\":float(x[2]),\"low\":float(x[3]),\n            \"close\":float(x[4]),\"volume\":float(x[5]),\"close_ts\":int(x[6]),\"taker_buy\":float(x[9]),\n        }\n        if b[\"close_ts\"]<cutoff: out.append(b)\n    return out\n\ndef signal_at(bs,i,cfg):\n    lb=int(cfg[\"signal_lookback_bars\"])\n    if i<lb:return None\n    b=bs[i]\n    base=bs[i-lb][\"close\"]\n    peak=max(x[\"high\"] for x in bs[i-lb+1:i+1])\n    rise=peak/base-1 if base>0 else 0\n    buy_share=b[\"taker_buy\"]/b[\"volume\"] if b[\"volume\"]>0 else 0.5\n    if not (rise>=float(cfg[\"signal_rise_threshold\"]) and b[\"close\"]<b[\"open\"] and buy_share<float(cfg[\"signal_taker_buy_share_max\"])):\n        return None\n    entry_ref=b[\"close\"]\n    return {\n        \"signal_open_ts\":b[\"open_ts\"],\n        \"signal_close_ts\":b[\"close_ts\"],\n        \"rise\":rise,\n        \"buy_share\":buy_share,\n        \"entry_reference\":entry_ref,\n        \"tp_reference\":entry_ref*(1-float(cfg[\"tp_pct\"])),\n        \"adverse_alert_reference\":entry_ref*(1+float(cfg[\"adverse_alert_pct\"])),\n    }\n\ndef tick():\n    ROOT.mkdir(parents=True,exist_ok=True)\n    with LOCK.open(\"a+\") as lf:\n        fcntl.flock(lf,fcntl.LOCK_EX)\n        cfg=load_cfg(); s=load_state(); bs=bars(); acct=account()\n        if int(cfg[\"account_index\"])!=ACCOUNT_INDEX or int(cfg[\"api_key_index\"])!=API_KEY_INDEX:\n            raise RuntimeError(\"dedicated account identity mismatch\")\n        if len(bs)<20: raise RuntimeError(\"not enough bars\")\n        if s[\"last_processed_close_ts\"]==0:\n            s[\"last_processed_close_ts\"]=bs[-1][\"close_ts\"]\n            save_state(s)\n            print(json.dumps({\"ok\":True,\"initialized\":True,\"account\":acct},ensure_ascii=False))\n            return\n        new=[i for i,b in enumerate(bs) if b[\"close_ts\"]>s[\"last_processed_close_ts\"]]\n        for i in new:\n            b=bs[i]\n            if b[\"open_ts\"]>=int(s.get(\"cooldown_until_ts\") or 0):\n                sig=signal_at(bs,i,cfg)\n                if sig:\n                    s[\"stats\"][\"signals\"]+=1\n                    s[\"active_signal\"]=sig\n                    s[\"cooldown_until_ts\"]=b[\"open_ts\"]+int(cfg[\"cooldown_minutes\"])*60*1000\n                    emit(\"SHORT_SIGNAL\",**sig,qty_eth=float(cfg[\"max_short_eth\"]),account_index=ACCOUNT_INDEX)\n            s[\"last_processed_close_ts\"]=b[\"close_ts\"]\n        save_state(s)\n        print(json.dumps({\"ok\":True,\"new_bars\":len(new),\"account\":acct,\"active_signal\":s.get(\"active_signal\"),\"stats\":s[\"stats\"]},ensure_ascii=False))\n\ndef status():\n    print(json.dumps({\n        \"account_index\":ACCOUNT_INDEX,\n        \"api_key_index\":API_KEY_INDEX,\n        \"execution\":\"LOCKED_NO_ORDERS\",\n        \"config\":load_cfg(),\n        \"account\":account(),\n        \"state\":load_state(),\n    },ensure_ascii=False,indent=2))\n\nif __name__==\"__main__\":\n    ap=argparse.ArgumentParser();ap.add_argument(\"--status\",action=\"store_true\");a=ap.parse_args()\n    status() if a.status else tick()\n",encoding="utf-8")
os.chmod(BOT,0o700)
subprocess.run(["python3","-m","py_compile",str(BOT)],check=True)
subprocess.run(["python3",str(BOT)],cwd=str(ROOT),check=True,timeout=40)

cron="* * * * * cd /home/apr/eth-hedge-live && python3 /home/apr/eth-hedge-live/monitor_bot.py >> /home/apr/eth-hedge-live/cron.log 2>&1"
r=subprocess.run(["crontab","-l"],text=True,capture_output=True)
lines=r.stdout.splitlines() if r.returncode==0 else []
lines=[x for x in lines if "/home/apr/eth-hedge-live/monitor_bot.py" not in x]
lines.append(cron)
subprocess.run(["crontab","-"],input="\n".join(lines).rstrip()+"\n",text=True,check=True)
print("ETH_HEDGE_MONITOR_DEPLOY_OK")
print("account_index = 749122")
print("api_key_index = 45")
print("execution = LOCKED_NO_ORDERS")
print("balance =",a.get("available_balance"))
print("collateral =",a.get("collateral"))
print("positions =",pos)
