#!/usr/bin/env python3
"""
Crypto Balance Dashboard – config.json persistence
=================================================

* Хранит Binance API‑ключи и on‑chain адреса в **config.json** рядом с
  приложением. Конфигурация доступна для редактирования через страницу
  **Add / Edit Sources**.
* Иллюстрация структуры файла:
  ```json
  {
    "accounts": {
      "Sub1": {"API_KEY": "…", "API_SECRET": "…"}
    },
    "addresses": {
      "MyETH": {"CHAIN": "ETH", "ADDRESS": "0x…"}
    }
  }
  ```
* При первом запуске, если файла нет, создаётся пустой.
* Все изменения из веб‑формы сразу пишутся в файл, так что они
  сохраняются между рестартами контейнера Streamlit Cloud.
* **Внимание:** ключи лежат в открытом виде на файловой системе контейнера.
  Для публичных развёртываний лучше держать экземпляр приватным.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

import requests
import streamlit as st

# -----------------------------------------------------------------------------
# Constants & file paths
# -----------------------------------------------------------------------------
# save config in writable home dir on Streamlit Cloud
BASE_DIR = Path.home() / ".crypto_dashboard"
BASE_DIR.mkdir(exist_ok=True)
CONFIG_PATH = BASE_DIR / "config.json"

API_BINANCE = "https://api.binance.com"
API_DEBANK = "https://openapi.debank.com"
API_SOLSCAN = "https://public-api.solscan.io"
API_BLOCKCYPHER = "https://api.blockcypher.com/v1/btc/main"

# -----------------------------------------------------------------------------
# Config helpers
# -----------------------------------------------------------------------------

def load_config() -> Dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            st.error("config.json is corrupted → resetting")
    return {"accounts": {}, "addresses": {}}


def save_config(cfg: Dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        st.error(f"Failed to save config: {e}")(json.dumps(cfg, indent=2))

# -----------------------------------------------------------------------------
# Price helper with cache (USD)
# -----------------------------------------------------------------------------

@st.cache_data(ttl=300)
def price_usdt(asset: str) -> float:
    if asset.upper() in ("USDT", "BUSD", "FDUSD"):
        return 1.0
    symbol = asset.upper() + "USDT"
    try:
        r = requests.get(f"{API_BINANCE}/api/v3/ticker/price", params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return 0.0

# -----------------------------------------------------------------------------
# Binance functions
# -----------------------------------------------------------------------------

def _ts() -> int:
    return int(time.time() * 1000)

def _sign(qs: str, secret: str) -> str:
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

def fetch_binance(key: str, secret: str) -> List[Dict]:
    qs = urllib.parse.urlencode({"timestamp": _ts()})
    url = f"{API_BINANCE}/api/v3/account?{qs}&signature={_sign(qs, secret)}"
    r = requests.get(url, headers={"X-Mbx-ApiKey": key}, timeout=10)
    r.raise_for_status()
    data = r.json()
    out = []
    for b in data.get("balances", []):
        tot = float(b["free"]) + float(b["locked"])
        if tot:
            sym = b["asset"]
            out.append({
                "asset": sym,
                "amount": tot,
                "usd": tot * price_usdt(sym),
                "src": "Binance",
            })
    return out

# -----------------------------------------------------------------------------
# ETH via DeBank
# -----------------------------------------------------------------------------

def fetch_eth(addr: str, debank_key: str | None) -> List[Dict]:
    headers = {"accept": "application/json"}
    if debank_key:
        headers["AccessKey"] = debank_key
    r = requests.get(f"{API_DEBANK}/v1/user/token_list?id={addr}&chain_id=eth", headers=headers, timeout=15)
    r.raise_for_status()
    tokens = r.json()
    out = []
    for t in tokens:
        amt = t.get("amount", 0)
        if amt:
            sym = t.get("symbol", t["id"])
            price = t.get("price", price_usdt(sym))
            out.append({"asset": sym, "amount": amt, "usd": amt * price, "src": "ETH"})
    return out

# -----------------------------------------------------------------------------
# SOL via Solscan
# -----------------------------------------------------------------------------

def fetch_sol(addr: str) -> List[Dict]:
    """Fetch SPL‑токены через Solscan. Некоторые адреса отдают 404 на старом
    эндпоинте (?account=). Пробуем пару вариантов и игнорируем 404 как «пусто»."""
    variants = [
        f"{API_SOLSCAN}/account/tokens?account={addr}",
        f"{API_SOLSCAN}/account/tokens?address={addr}",  # новый формат
    ]
    tokens = None
    for url in variants:
        r = requests.get(url, timeout=15)
        if r.status_code == 404:
            continue  # попробуем другой вариант
        r.raise_for_status()
        tokens = r.json()
        break
    if tokens is None:
        return []  # ничего не нашли

    out = []
    for t in tokens:
        try:
            lamports = int(t["tokenAmount"]["amount"])
            dec = t["tokenAmount"].get("decimals", 0)
            amt = lamports / (10 ** dec) if dec else 0
        except (KeyError, ValueError):
            continue
        if amt:
            sym = t.get("tokenSymbol") or t.get("mintAddress", "")[:6]
            out.append({
                "asset": sym,
                "amount": amt,
                "usd": amt * price_usdt(sym),
                "src": "SOL",
            })
    return out

# -----------------------------------------------------------------------------
# BTC via BlockCypher
# -----------------------------------------------------------------------------

def fetch_btc(addr: str) -> List[Dict]:
    r = requests.get(f"{API_BLOCKCYPHER}/addrs/{addr}/balance", timeout=10)
    r.raise_for_status()
    sat = r.json().get("balance", 0)
    btc = sat / 1e8
    return [{"asset": "BTC", "amount": btc, "usd": btc * price_usdt("BTC"), "src": "BTC"}]

# -----------------------------------------------------------------------------
# Streamlit app
# -----------------------------------------------------------------------------

st.set_page_config("Crypto Dashboard", "💰", layout="wide")

if "cfg" not in st.session_state:
    st.session_state.cfg = load_config()

debank_key = st.secrets.get("DEBANK_KEY", None)  # опционально из Secrets

page = st.sidebar.selectbox("Page", ("Dashboard", "Accounts", "Add / Edit Sources"))

cfg = st.session_state.cfg  # shortcut

# -----------------------------------------------------------------------------
# Add / Edit page
# -----------------------------------------------------------------------------
if page == "Add / Edit Sources":
    st.header("🔧 Manage Sources")
    tab1, tab2 = st.tabs(["Binance", "Addresses"])

    with tab1:
        st.subheader("Add Binance Sub‑Account")
        with st.form("add_bin"):
            label = st.text_input("Label")
            key = st.text_input("API Key", type="password")
            sec = st.text_input("Secret", type="password")
            ok = st.form_submit_button("Save")
        if ok and label and key and sec:
            cfg.setdefault("accounts", {})[label] = {"API_KEY": key, "API_SECRET": sec}
            save_config(cfg)
            st.success("Saved")

        if cfg.get("accounts"):
            st.write("### Current accounts")
            for l in list(cfg["accounts"].keys()):
                col1, col2 = st.columns((4,1))
                col1.write(l)
                if col2.button("❌", key=f"delacc_{l}"):
                    del cfg["accounts"][l]
                    save_config(cfg)
                    st.experimental_rerun()

    with tab2:
        st.subheader("Add Address")
        with st.form("add_addr"):
            label = st.text_input("Label", key="lab2")
            chain = st.selectbox("Chain", ("ETH", "SOL", "BTC"))
            addr = st.text_input("Address")
            ok2 = st.form_submit_button("Save")
        if ok2 and label and addr:
            cfg.setdefault("addresses", {})[label] = {"CHAIN": chain, "ADDRESS": addr}
            save_config(cfg)
            st.success("Saved")

        if cfg.get("addresses"):
            st.write("### Current addresses")
            for l in list(cfg["addresses"].keys()):
                col1, col2 = st.columns((4,1))
                col1.write(f"{l} ({cfg['addresses'][l]['CHAIN']})")
                if col2.button("❌", key=f"deladdr_{l}"):
                    del cfg["addresses"][l]
                    save_config(cfg)
                    st.experimental_rerun()

# -----------------------------------------------------------------------------
# Dashboard page
# -----------------------------------------------------------------------------
elif page == "Dashboard":
    st.header("📊 Aggregated Dashboard")
    agg: Dict[str, float] = {}

    # Binance
    for lab, cred in cfg.get("accounts", {}).items():
        try:
            for b in fetch_binance(cred["API_KEY"], cred["API_SECRET"]):
                agg[b["asset"]] = agg.get(b["asset"], 0) + b["usd"]
        except Exception as e:
            st.error(f"{lab}: {e}")

    # Addresses
    for lab, info in cfg.get("addresses", {}).items():
        try:
            if info["CHAIN"] == "ETH":
                toks = fetch_eth(info["ADDRESS"], debank_key)
            elif info["CHAIN"] == "SOL":
                toks = fetch_sol(info["ADDRESS"])
            else:
                toks = fetch_btc(info["ADDRESS"])
            for b in toks:
                agg[b["asset"]] = agg.get(b["asset"], 0) + b["usd"]
        except Exception as e:
            st.error(f"{lab}: {e}")

    total = sum(agg.values())
    st.metric("≈ Total USD", f"{total:,.2f}")
    rows = [{"Asset": a, "USD": v} for a, v in sorted(agg.items(), key=lambda x: x[1], reverse=True)]
    st.dataframe(rows, hide_index=True, use_container_width=True)

# -----------------------------------------------------------------------------
# Accounts detailed page
# -----------------------------------------------------------------------------
else:
    st.header("🗂 Detailed Balances")

    for lab, cred in cfg.get("accounts", {}).items():
        with st.expander(f"Binance – {lab}"):
            try:
                data = fetch_binance(cred["API_KEY"], cred["API_SECRET"])
                st.dataframe(data, hide_index=True, use_container_width=True)
            except Exception as e:
                st.error(str(e))

    for lab, info in cfg.get("addresses", {}).items():
        with st.expander(f"{info['CHAIN']} – {lab}"):
            try:
                if info["CHAIN"] == "ETH":
                    data = fetch_eth(info["ADDRESS"], debank_key)
                elif info["CHAIN"] == "SOL":
                    data = fetch_sol(info["ADDRESS"])
                else:
                    data = fetch_btc(info["ADDRESS"])
                st.dataframe(data, hide_index=True, use_container_width=True)
            except Exception as e:
                st.error(str(e))

st.caption("Data cached 5 min • File‑based config.json persistence • Use at your own risk")
