#!/usr/bin/env python3
from __future__ import annotations
import json, os, pathlib, subprocess, urllib.parse, urllib.request, time

ROOT=pathlib.Path("/home/apr/eth-hedge-live")
ENV=ROOT/".env"
CFG=ROOT/"config.json"
BOT=ROOT/"monitor_bot.py"
STATE=ROOT/"state.json"
LOG=ROOT/"cron.log"
ACCOUNT=749122
KEY_INDEX=45

out={"ok":False,"orders_sent":0,"secret_values_printed":False,"checks":{},"errors":[]}

def fail(name,msg):
    out["checks"][name]=False
    out["errors"].append(f"{name}: {msg}")

def ok(name,val=True):
    out["checks"][name]=val

try:
    ok("root_present", ROOT.is_dir())
    ok("env_present", ENV.is_file())
    ok("config_present", CFG.is_file())
    ok("monitor_present", BOT.is_file())
    if not all([ROOT.is_dir(),ENV.is_file(),CFG.is_file(),BOT.is_file()]):
        raise RuntimeError("required files missing")

    st=ENV.stat()
    ok("env_mode_secure", (st.st_mode & 0o077)==0)
    vals={}
    private_present=False
    for line in ENV.read_text(errors="ignore").splitlines():
        s=line.strip()
        if not s or s.startswith("#") or "=" not in s: continue
        k,v=s.split("=",1); k=k.strip(); v=v.strip().strip("'\"")
        if k in {"LIGHTER_BASE_URL","LIGHTER_ACCOUNT_INDEX","LIGHTER_API_KEY_INDEX"}:
            vals[k]=v
        elif k=="LIGHTER_API_PRIVATE_KEY_HEX":
            private_present=bool(v)
    ok("account_index_749122", vals.get("LIGHTER_ACCOUNT_INDEX")=="749122")
    ok("api_key_index_45", vals.get("LIGHTER_API_KEY_INDEX")=="45")
    ok("base_url_mainnet", vals.get("LIGHTER_BASE_URL")=="https://mainnet.zklighter.elliot.ai")
    ok("private_key_present", private_present)

    cfg=json.loads(CFG.read_text())
    expected={
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
        "auto_timeout_exit":False,
    }
    mismatches={k:{"got":cfg.get(k),"expected":v} for k,v in expected.items() if cfg.get(k)!=v}
    ok("config_exact", not mismatches)
    if mismatches: out["config_mismatches"]=mismatches

    text=BOT.read_text(errors="ignore")
    forbidden=["SignerClient","create_order","create_market_order","create_tp_order","LIGHTER_API_PRIVATE_KEY_HEX","cancel_all_orders"]
    found=[x for x in forbidden if x in text]
    ok("monitor_has_no_order_code", not found)
    if found: out["forbidden_markers"]=found

    cp=subprocess.run(["python3","-m","py_compile",str(BOT)],text=True,capture_output=True,timeout=20)
    ok("python_compile", cp.returncode==0)
    if cp.returncode!=0: out["errors"].append((cp.stderr or cp.stdout)[-1000:])

    cr=subprocess.run(["crontab","-l"],text=True,capture_output=True,timeout=10)
    lines=[x for x in cr.stdout.splitlines() if "/home/apr/eth-hedge-live/monitor_bot.py" in x] if cr.returncode==0 else []
    ok("cron_exactly_one", len(lines)==1)
    out["cron_count"]=len(lines)

    status=subprocess.run(["python3",str(BOT),"--status"],cwd=str(ROOT),text=True,capture_output=True,timeout=30)
    ok("status_command", status.returncode==0)
    if status.returncode==0:
        try:
            sj=json.loads(status.stdout)
            out["monitor_status"]={
                "execution":sj.get("execution"),
                "account_index":sj.get("account_index"),
                "api_key_index":sj.get("api_key_index"),
                "state":sj.get("state"),
                "account":sj.get("account"),
            }
            ok("status_locked_no_orders", sj.get("execution")=="LOCKED_NO_ORDERS")
        except Exception as e:
            fail("status_json",str(e))
    else:
        out["errors"].append((status.stderr or status.stdout)[-1500:])

    # Current public Lighter account snapshot.
    url="https://mainnet.zklighter.elliot.ai/api/v1/account?"+urllib.parse.urlencode({
        "by":"index","value":"749122","active_only":"false"
    })
    req=urllib.request.Request(url,headers={"User-Agent":"eth-hedge-final-audit/1.0"})
    with urllib.request.urlopen(req,timeout=15) as r:
        j=json.load(r)
    a=(j.get("accounts") or [{}])[0]
    positions=[]
    for p in a.get("positions") or []:
        try:q=float(p.get("position") or 0)
        except Exception:q=0.0
        if abs(q)>1e-12:
            positions.append({
                "symbol":p.get("symbol"),
                "market_id":p.get("market_id"),
                "sign":p.get("sign"),
                "position":q,
                "avg_entry_price":p.get("avg_entry_price"),
                "liquidation_price":p.get("liquidation_price"),
            })
    bal=float(a.get("available_balance") or 0)
    col=float(a.get("collateral") or 0)
    out["account_snapshot"]={"available_balance":bal,"collateral":col,"positions":positions}
    ok("account_flat", positions==[])
    ok("collateral_above_80", col>=80.0)

    # State freshness: initialized and recent cron/log activity.
    if STATE.is_file():
        ss=json.loads(STATE.read_text())
        out["state_summary"]={
            "last_processed_close_ts":ss.get("last_processed_close_ts"),
            "cooldown_until_ts":ss.get("cooldown_until_ts"),
            "active_signal":ss.get("active_signal"),
            "stats":ss.get("stats"),
        }
        ok("state_initialized", int(ss.get("last_processed_close_ts") or 0)>0)
    else:
        fail("state_initialized","state.json missing")

    if LOG.exists():
        age=time.time()-LOG.stat().st_mtime
        out["cron_log_age_seconds"]=round(age,1)
        ok("cron_log_recent", age<180)
        tail=LOG.read_text(errors="ignore")[-5000:]
        ok("cron_no_traceback", "Traceback (most recent call last)" not in tail)
    else:
        fail("cron_log_recent","cron.log missing")

    # Signer binding only; no transaction creation.
    py=ROOT/".venv/bin/python"
    if py.is_file():
        probe=r"""
import asyncio
from pathlib import Path
from lighter import SignerClient
async def main():
    vals={}
    for line in Path(".env").read_text(errors="ignore").splitlines():
        s=line.strip()
        if not s or s.startswith("#") or "=" not in s: continue
        k,v=s.split("=",1); vals[k.strip()]=v.strip().strip("'\"")
    c=SignerClient(vals["LIGHTER_BASE_URL"],749122,{45:vals["LIGHTER_API_PRIVATE_KEY_HEX"]})
    try:
        err=c.check_client()
        if err is not None: raise SystemExit(2)
        print("SIGNER_OK")
    finally:
        await c.close()
asyncio.run(main())
"""
        sp=subprocess.run([str(py),"-c",probe],cwd=str(ROOT),text=True,capture_output=True,timeout=30)
        ok("signer_binding", sp.returncode==0 and "SIGNER_OK" in sp.stdout)
        if not out["checks"]["signer_binding"]:
            out["errors"].append((sp.stderr or sp.stdout)[-1000:])
    else:
        fail("signer_binding","dedicated venv missing")

    critical=[
        "root_present","env_present","config_present","monitor_present","env_mode_secure",
        "account_index_749122","api_key_index_45","base_url_mainnet","private_key_present",
        "config_exact","monitor_has_no_order_code","python_compile","cron_exactly_one",
        "status_command","status_locked_no_orders","account_flat","collateral_above_80",
        "state_initialized","cron_log_recent","cron_no_traceback","signer_binding"
    ]
    out["ok"]=all(out["checks"].get(k) is True for k in critical)
except Exception as e:
    out["errors"].append(type(e).__name__+": "+str(e)[:1000])

print(json.dumps(out,ensure_ascii=False,indent=2))
