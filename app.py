#!/usr/bin/env python3
"""
Binance Balance Dashboard – Streamlit Community Cloud ready 🏄‍♀️
=============================================================

* Три «страницы» в одном файле (`sidebar.selectbox`):
  1. **Dashboard** – суммарная сводка по всем аккаунтам.
  2. **Accounts** – детальные балансы каждого аккаунта.
  3. **Add Keys** – форма для ввода/тестирования API‑ключей прямо в браузере.

* **Хранилище ключей**
  - Во время сессии: `st.session_state["accounts"]`.
  - Постоянно на Cloud: секция `[accounts]` в `Secrets`. На странице «Add Keys» после добавления выводится TOML‑сниппет, который можно скопировать в Settings → Secrets.

* **Зависимости**: `streamlit`, `requests`.

Размещаешь репозиторий на GitHub → Deploy to Streamlit Community Cloud → в "Secrets" добавляешь блок `[accounts]` → Profit.
"""

import time
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timezone
from functools import lru_cache

import requests
import streamlit as st

API_BASE = "https://api.binance.com"

# -----------------------------------------------------------------------------
# Low‑level Binance helpers
# -----------------------------------------------------------------------------

def _timestamp_ms() -> int:
    return int(time.time() * 1000)


def _sign(query_string: str, secret: str) -> str:
    return hmac.new(secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()


def _call_binance(endpoint: str, key: str, secret: str):
    qs = urllib.parse.urlencode({"timestamp": _timestamp_ms()})
    signature = _sign(qs, secret)
    url = f"{API_BASE}{endpoint}?{qs}&signature={signature}"
    headers = {"X-Mbx-ApiKey": key, "Accept": "application/json"}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


# -----------------------------------------------------------------------------
# Price cache (USDT‑пары) – для быстрой грубой оценки портфеля
# -----------------------------------------------------------------------------

@st.cache_data(ttl=300)
def get_price_usdt(asset: str) -> float:
    if asset.upper() in ("USDT", "BUSD", "FDUSD"):
        return 1.0
    symbol = asset.upper() + "USDT"
    try:
        r = requests.get(f"{API_BASE}/api/v3/ticker/price", params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return 0.0  # неизвестная пара – вернём 0


# -----------------------------------------------------------------------------
# Балансы одного аккаунта
# -----------------------------------------------------------------------------

@lru_cache(maxsize=32)
def fetch_balances(api_key: str, api_sec: str):
    data = _call_binance("/api/v3/account", api_key, api_sec)
    balances = []
    for b in data.get("balances", []):
        free = float(b["free"])
        locked = float(b["locked"])
        total = free + locked
        if total > 0:
            balances.append({
                "asset": b["asset"],
                "free": free,
                "locked": locked,
                "total": total,
                "usd": total * get_price_usdt(b["asset"]),
            })
    return balances


# -----------------------------------------------------------------------------
# Streamlit UI helpers
# -----------------------------------------------------------------------------

st.set_page_config(page_title="Binance Balances", layout="wide", page_icon="💰")

# Bootstrap session_state with secrets
if "accounts" not in st.session_state:
    st.session_state["accounts"] = {}
    if "accounts" in st.secrets:
        for label, creds in st.secrets["accounts"].items():
            st.session_state.accounts[label] = {
                "API_KEY": creds["API_KEY"],
                "API_SECRET": creds["API_SECRET"],
            }

page = st.sidebar.selectbox("Навигация", ("Dashboard", "Accounts", "Add Keys"))

# -----------------------------------------------------------------------------
# Page: Add Keys
# -----------------------------------------------------------------------------

if page == "Add Keys":
    st.header("➕ Add Sub‑Account")
    with st.form("add_sub"):
        label = st.text_input("Label (Sub Name)")
        api_key = st.text_input("API Key", type="password")
        api_sec = st.text_input("Secret Key", type="password")
        submitted = st.form_submit_button("Save (session)")
    if submitted and label and api_key and api_sec:
        st.session_state.accounts[label] = {"API_KEY": api_key, "API_SECRET": api_sec}
        st.success(f"{label} добавлен, можно тестировать сразу.")
        st.markdown("**Чтобы сохранить навсегда:** скопируй блок ниже в _Secrets_ на Streamlit Cloud → перезапусти app.")
        toml = f"""[accounts.{label}]
API_KEY = \"{api_key}\"
API_SECRET = \"{api_sec}\""""""
        st.code(toml, language="toml")

    if st.session_state.accounts:
        st.subheader("Текущие аккаунты (session + secrets)")
        table = [{"label": k, "key": "•" * 6, "secret": "•" * 6} for k in st.session_state.accounts]
        st.table(table)
    else:
        st.info("Пока нет ни одного аккаунта.")

# -----------------------------------------------------------------------------
# Page: Dashboard (aggregate)
# -----------------------------------------------------------------------------

elif page == "Dashboard":
    st.header("📊 Dashboard – Сводка по всем аккаунтам")
    if not st.session_state.accounts:
        st.warning("Добавь хотя бы один аккаунт на странице **Add Keys**.")
        st.stop()

    agg = {}
    total_usd = 0.0
    for label, creds in st.session_state.accounts.items():
        try:
            bals = fetch_balances(creds["API_KEY"], creds["API_SECRET"])
        except Exception as e:
            st.error(f"{label}: {e}")
            continue
        for b in bals:
            a = b["asset"]
            if a not in agg:
                agg[a] = 0.0
            agg[a] += b["total"]
        total_usd += sum(x["usd"] for x in bals)

    col1, col2 = st.columns(2)
    col1.metric("Активов", len(agg))
    col2.metric("Примерно USD", f"{total_usd:,.2f}")

    # Таблица агрегированных балансов
    rows = [{"Asset": a, "Total": t, "≈USD": t * get_price_usdt(a)} for a, t in agg.items()]
    rows.sort(key=lambda r: r["≈USD"], reverse=True)
    st.dataframe(rows, use_container_width=True, hide_index=True)

# -----------------------------------------------------------------------------
# Page: Accounts (per‑account view)
# -----------------------------------------------------------------------------

elif page == "Accounts":
    st.header("🗂 Балансы по аккаунтам")
    if not st.session_state.accounts:
        st.warning("Добавь хотя бы один аккаунт на странице **Add Keys**.")
        st.stop()

    for label, creds in st.session_state.accounts.items():
        with st.expander(label, expanded=False):
            try:
                bals = fetch_balances(creds["API_KEY"], creds["API_SECRET"])
            except Exception as e:
                st.error(str(e))
                continue
            if not bals:
                st.write("Пусто.")
                continue
            st.dataframe(bals, hide_index=True, use_container_width=True)
