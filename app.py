"""
Cold-Chain Smart Contract Dashboard — Streamlit + live Web3 oracle backend
=============================================================================
Run with:   streamlit run app2.py

This dashboard is now wired directly to a DEPLOYED ColdChainAgreement.sol
contract running on a Hardhat node. There is no simulated blockchain layer
and smart_contract.py is no longer imported or used as a source of truth.

Flow:  Streamlit UI  ->  Web3 backend/oracle (this file)  ->
       ColdChainAgreement.sol  ->  Hardhat blockchain

- Agreement identity, signatures, status, temperature range, violations and
  settlement are all READ from the contract (never from local Python state).
- Signing, activating, starting tracking, recording temperature and
  completing the shipment are all done by SENDING REAL TRANSACTIONS and
  waiting for their receipts.
- The only "simulation" left is the fan/temperature sensor drift, which
  stands in for a real IoT sensor. Every simulated reading that matters is
  reported on-chain via recordTemperature(); the contract alone decides
  whether it's a violation.
- Every tx hash / block number shown anywhere in the UI comes from a real
  transaction receipt or a real event log — nothing is fabricated. If the
  chain is unreachable or a transaction fails, the UI shows an error
  instead of inventing data.
"""

import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
from dotenv import load_dotenv
from web3 import Web3

# ---------------------------------------------------------------------------
# Environment / chain configuration
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent

# Load .env explicitly from the app's own folder — don't rely on Streamlit's
# working directory, which is often the directory `streamlit run` was
# launched from, not this file's directory.
load_dotenv(APP_DIR / ".env")


def _clean_env(name: str):
    """Read an env var and strip whitespace/quotes a person may have pasted in."""
    v = os.getenv(name)
    if v is None:
        return None
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v or None


RPC_URL = _clean_env("RPC_URL") or "http://127.0.0.1:8545"
CONTRACT_ADDRESS = _clean_env("CONTRACT_ADDRESS")
ORACLE_PRIVATE_KEY = _clean_env("ORACLE_PRIVATE_KEY")
SHIPPER_PRIVATE_KEY = _clean_env("SHIPPER_PRIVATE_KEY")
TRANSPORTER_PRIVATE_KEY = _clean_env("TRANSPORTER_PRIVATE_KEY")

# ABI lives next to this file. deploy.js writes it as contract_abi.json at the
# project root — copy it here. No deployment.json is read anywhere: the
# contract address comes ONLY from CONTRACT_ADDRESS in .env, as required.
#
# Resolution order (handles browser-downloaded copies that get a suffix like
# "contract_abi (3).json" appended to the filename):
#   1. contract_abi.json (the canonical name deploy.js writes)
#   2. any contract_abi*.json file found next to app2.py (first match)
def _resolve_abi_path() -> Path:
    canonical = APP_DIR / "contract_abi.json"
    if canonical.exists():
        return canonical
    candidates = sorted(APP_DIR.glob("contract_abi*.json"))
    if candidates:
        return candidates[0]
    return canonical  # doesn't exist — get_chain() will report a clear error


ABI_PATH = _resolve_abi_path()

TICK_SEC = 1.2

# Solidity AgreementStatus enum, in order (see ColdChainAgreement.sol)
STATUS_NAMES = [
    "Draft", "WaitingForShipperSignature", "WaitingForTransporterSignature",
    "Signed", "FundsLocked", "Active", "ViolationActive",
    "ViolationResolved", "ShipmentCompleted", "Settled",
]
STATUS_LABELS = {
    "Draft": "DRAFT",
    "WaitingForShipperSignature": "WAITING_FOR_SHIPPER_SIGNATURE",
    "WaitingForTransporterSignature": "WAITING_FOR_TRANSPORTER_SIGNATURE",
    "Signed": "SIGNED",
    "FundsLocked": "FUNDS_LOCKED",
    "Active": "TRACKING",
    "ViolationActive": "VIOLATION_ACTIVE",
    "ViolationResolved": "VIOLATION_RESOLVED",
    "ShipmentCompleted": "SETTLED",
    "Settled": "SETTLED",
}
TRACKING_STATUSES = ("Active", "ViolationActive", "ViolationResolved")

COLORS = {
    "bg": "#14171C", "surface": "#1B1F26", "surface2": "#23272F",
    "border": "#2E333C", "text": "#EDEFF2", "text2": "#8B93A1", "muted": "#5C636F",
    "teal": "#00D9B5", "coral": "#FF6B4A", "amber": "#FFB84A",
}

st.set_page_config(page_title="Cold-Chain Smart Contract", layout="wide")

st.markdown(f"""
<style>
.stApp {{ background-color: {COLORS['bg']}; color: {COLORS['text']}; }}
section[data-testid="stSidebar"] {{ display: none; }}
h1, h2, h3, h4, p, span, label, div {{ color: {COLORS['text']}; }}
.stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {COLORS['border']}; }}
.stTabs [data-baseweb="tab"] {{ background-color: transparent; color: {COLORS['text2']}; }}
.stTabs [aria-selected="true"] {{ color: {COLORS['text']} !important; border-bottom: 2px solid {COLORS['teal']} !important; }}
div[data-testid="stMetric"] {{ background-color: {COLORS['surface2']}; border: 1px solid {COLORS['border']}; border-radius: 10px; padding: 12px; }}
div[data-testid="stMetricLabel"] {{ color: {COLORS['muted']}; }}
div[data-testid="stMetricValue"] {{ color: {COLORS['text']}; font-family: 'IBM Plex Mono', monospace; }}
div[data-testid="stVerticalBlockBorderWrapper"] {{ background-color: {COLORS['surface']}; border: 1px solid {COLORS['border']} !important; border-radius: 10px; }}
.stButton>button {{ background-color: {COLORS['surface2']}; color: {COLORS['text2']}; border: 1px solid {COLORS['border']}; border-radius: 8px; }}
.stButton>button[kind="primary"] {{ background-color: {COLORS['teal']}; color: #08201B; border: none; font-weight: 600; }}
.stNumberInput input, .stTextInput input {{ background-color: {COLORS['surface2']}; color: {COLORS['text']}; border: 1px solid {COLORS['border']}; font-family: 'IBM Plex Mono', monospace; }}
.stCaption, [data-testid="stCaptionContainer"] {{ color: {COLORS['muted']}; }}
code {{ background-color: {COLORS['surface2']} !important; color: {COLORS['text2']} !important; }}
hr {{ border-color: {COLORS['border']}; }}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Small presentational helpers (unchanged from the original dashboard)
# ---------------------------------------------------------------------------
def badge(text: str, ok: bool):
    bg = "#0A3D34" if ok else "#442019"
    fg = COLORS["teal"] if ok else COLORS["coral"]
    icon = "\u2705" if ok else "\u26a0\ufe0f"
    st.markdown(
        f"<div style='display:inline-flex;align-items:center;gap:8px;padding:8px 14px;"
        f"border-radius:999px;background:{bg};border:1px solid {fg};font-family:monospace;"
        f"font-size:13px;color:{fg}'>{icon} {text}</div>", unsafe_allow_html=True)


def dial(temp: float, ok: bool):
    color = COLORS["teal"] if ok else COLORS["coral"]
    st.markdown(
        f"<div style='width:140px;height:140px;border-radius:50%;border:3px solid {color};"
        f"display:flex;flex-direction:column;align-items:center;justify-content:center;"
        f"background:{COLORS['surface2']};margin:0 auto 12px'>"
        f"<div style='font-family:monospace;font-size:30px;font-weight:600;color:{color}'>{temp:.1f}</div>"
        f"<div style='font-size:11px;color:{COLORS['muted']}'>\u00b0C</div></div>", unsafe_allow_html=True)


def pill(text: str, tone: str):
    colors = {"teal": (COLORS["teal"], "#0A3D34"), "amber": (COLORS["amber"], "#463319"),
              "coral": (COLORS["coral"], "#442019")}
    fg, bg = colors[tone]
    st.markdown(
        f"<span style='padding:4px 10px;border-radius:999px;background:{bg};border:1px solid {fg};"
        f"color:{fg};font-family:monospace;font-size:11px'>{text}</span>", unsafe_allow_html=True)


def chain_tag(text: str, live: bool = True):
    """Small tag honestly labelling data as real on-chain vs a local error state."""
    color = COLORS["teal"] if live else COLORS["coral"]
    st.markdown(
        f"<span style='padding:2px 8px;border-radius:999px;background:{COLORS['surface2']};"
        f"border:1px solid {color};color:{color};font-family:monospace;"
        f"font-size:10px'>{text}</span>", unsafe_allow_html=True)


def format_duration(sec) -> str:
    s = max(0, round(sec or 0))
    return f"{s // 60:02d}:{s % 60:02d}"


def inr(n) -> str:
    return f"\u20b9{round(n or 0):,}"


def short(x: str, n: int = 6) -> str:
    if not x:
        return "\u2014"
    return f"{x[:2 + n]}\u2026{x[-4:]}"


def gen_drift(fan_on: bool, drift: float) -> float:
    noise = (random.random() - 0.5) * 0.4
    pull = -drift * 0.35 if fan_on else 0.18
    return drift + pull + noise


# ---------------------------------------------------------------------------
# Web3 / contract connection (cached across reruns within a server process)
# ---------------------------------------------------------------------------
def _load_abi():
    """Load contract_abi.json and return the bare ABI array, whatever shape the
    file is in. Accepts either a raw ABI array (what deploy.js writes via
    `artifact.abi`) or a full Hardhat/Truffle artifact object that wraps the
    array under an "abi" key — so a differently-exported ABI file still loads."""
    raw = json.loads(ABI_PATH.read_text())
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("abi"), list):
        return raw["abi"]
    raise ValueError(
        "contract_abi.json doesn't contain a recognizable ABI array "
        "(expected a JSON list, or an object with an \"abi\" list inside it)."
    )


@st.cache_resource(show_spinner=False)
def get_chain():
    errors = []

    if not CONTRACT_ADDRESS:
        errors.append("CONTRACT_ADDRESS is not set in .env.")
    if not ORACLE_PRIVATE_KEY:
        errors.append("ORACLE_PRIVATE_KEY is not set in .env.")
    if not SHIPPER_PRIVATE_KEY:
        errors.append("SHIPPER_PRIVATE_KEY is not set in .env.")
    if not TRANSPORTER_PRIVATE_KEY:
        errors.append("TRANSPORTER_PRIVATE_KEY is not set in .env.")
    if not ABI_PATH.exists():
        errors.append(f"ABI file not found at {ABI_PATH}. Copy contract_abi.json next to app2.py.")

    if errors:
        return {"ok": False, "errors": errors}

    try:
        contract_address = Web3.to_checksum_address(CONTRACT_ADDRESS)
    except Exception as e:
        return {"ok": False, "errors": [f"CONTRACT_ADDRESS '{CONTRACT_ADDRESS}' is not a valid address: {e}"]}

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    try:
        connected = w3.is_connected()
    except Exception as e:
        connected = False
        errors.append(str(e))

    if not connected:
        errors.append(f"Cannot reach a Hardhat RPC node at {RPC_URL}. Is `npx hardhat node` running?")
        return {"ok": False, "errors": errors}

    try:
        abi = _load_abi()
    except Exception as e:
        return {"ok": False, "errors": [f"Could not parse {ABI_PATH}: {e}"]}

    try:
        code = w3.eth.get_code(contract_address)
        if not code or code == b"" or code.hex() in ("0x", ""):
            return {"ok": False, "errors": [
                f"No contract code found at {contract_address} on {RPC_URL}. "
                f"Re-run `npx hardhat run scripts/deploy.js --network localhost` "
                f"(a Hardhat node restart wipes previously deployed contracts) "
                f"and update CONTRACT_ADDRESS in .env."
            ]}
    except Exception as e:
        return {"ok": False, "errors": [f"Could not check contract code at {contract_address}: {e}"]}

    try:
        contract = w3.eth.contract(address=contract_address, abi=abi)
        contract.functions.agreementId().call()  # sanity check: ABI actually matches deployed code
    except Exception as e:
        return {"ok": False, "errors": [
            f"Could not call the deployed contract at {contract_address} with this ABI: {e}. "
            f"Make sure contract_abi.json matches the deployed ColdChainAgreement.sol."
        ]}

    try:
        oracle_acct = w3.eth.account.from_key(ORACLE_PRIVATE_KEY)
        shipper_acct = w3.eth.account.from_key(SHIPPER_PRIVATE_KEY)
        transporter_acct = w3.eth.account.from_key(TRANSPORTER_PRIVATE_KEY)
    except Exception as e:
        return {"ok": False, "errors": [f"Invalid private key in .env: {e}"]}

    return {
        "ok": True, "errors": [], "w3": w3, "contract": contract, "address": contract_address,
        "oracle": oracle_acct, "shipper_acct": shipper_acct, "transporter_acct": transporter_acct,
    }


def send_tx(chain, account, fn):
    """Build, sign, send and wait for receipt of a contract call. Raises on failure."""
    w3 = chain["w3"]
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": nonce,
        "chainId": w3.eth.chain_id,
        "gasPrice": w3.eth.gas_price,
    })
    try:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    except Exception:
        tx["gas"] = 500_000
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Transaction {receipt.transactionHash.hex()} reverted on-chain")
    return receipt


def fetch_full_state(chain):
    """Single read-only snapshot of everything the UI needs, straight from Solidity."""
    c = chain["contract"]
    (agreement_id, shipper_addr, transporter_addr, min_temp, max_temp, deal_amount,
     status_int, shipper_signed, transporter_signed) = c.functions.getAgreementDetails().call()

    active_idx = c.functions.getActiveViolationIndex().call()
    violation_count = c.functions.getViolationCount().call()
    max_violation_duration = c.functions.maxViolationDurationSec().call()
    last_temp, last_ts, reading_count = c.functions.getLastReading().call()
    settled, final_pct, deducted_amt, final_amt = c.functions.getSettlementSummary().call()

    violations = []
    for i in range(violation_count):
        v = c.functions.getViolationDetails(i).call()
        violations.append({
            "index": i, "startTime": v[0], "endTime": v[1], "tempAtStartCentiC": v[2],
            "durationSec": v[3], "penaltyPercent": v[4], "deductedAmount": v[5], "resolved": v[6],
        })

    return {
        "agreement_id": agreement_id,
        "shipper_addr": shipper_addr, "transporter_addr": transporter_addr,
        "oracle_addr": c.functions.authorizedOracle().call(),
        "product_id": c.functions.productId().call(),
        "batch_id": c.functions.batchId().call(),
        "container_id": c.functions.containerId().call(),
        "min_temp": min_temp / 100.0, "max_temp": max_temp / 100.0,
        "deal_amount": deal_amount,
        "status_int": status_int, "status_name": STATUS_NAMES[status_int],
        "shipper_signed": shipper_signed, "transporter_signed": transporter_signed,
        "active_idx": active_idx, "violation_count": violation_count,
        "max_violation_duration": max_violation_duration,
        "last_temp": (last_temp / 100.0) if reading_count else None,
        "last_ts": last_ts, "reading_count": reading_count,
        "settled": settled, "final_pct": final_pct,
        "deducted_amt": deducted_amt, "final_amt": final_amt,
        "violations": violations,
    }


def live_penalty_preview(chain, deal_amount, duration_sec):
    """Ask Solidity's own pure calculatePenalty() for the tier — the tier table
    itself is never re-implemented in Python. Only the trivial (amount * pct/100)
    arithmetic, identical to the contract's own math, is done client-side for a
    live preview of an unresolved violation."""
    try:
        pct = chain["contract"].functions.calculatePenalty(int(duration_sec)).call()
    except Exception:
        pct = 0
    deducted = (deal_amount * pct) // 100
    final = deal_amount - deducted
    return pct, deducted, final


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def init_state():
    defaults = {
        # off-chain contact/display info only — never sent to the contract
        "shipper_name": "ABC Pharma Pvt. Ltd.",
        "shipper_email": "ops@abcpharma.demo",
        "shipper_phone": "+91 90000 00001",
        "shipper_address": "Plot 12, MIDC Industrial Area, Pune",
        "transporter_name": "XYZ Logistics",
        "transporter_email": "dispatch@xyzlogistics.demo",
        "transporter_phone": "+91 90000 00002",
        "transporter_address": "Warehouse 4, Bhiwandi Logistics Park",

        # local sensor/fan simulation (unchanged)
        "fan_on": True,
        "drift": 0.0,
        "history": [{"t": i, "temp": 27 + (random.random() - 0.5)} for i in range(18)],
        "tick": 18,
        "demo_speed": 1,

        # tx bookkeeping — real hashes/blocks only, populated after receipts
        "chain_busy": False,
        "chain_state": None,
        "chain_read_error": None,
        "tx_log": {},
        "violation_tx_map": {},
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def reset_demo():
    keys = list(st.session_state.keys())
    for k in keys:
        del st.session_state[k]
    init_state()


# ---------------------------------------------------------------------------
# One simulation tick — advances local temperature AND, if tracking is
# active on-chain, reports the reading via a real recordTemperature() tx.
# ---------------------------------------------------------------------------
def run_tick(chain):
    s = st.session_state
    new_drift = gen_drift(s.fan_on, s.drift)
    s.drift = max(-8.0, min(12.0, new_drift))
    t = 27 + s.drift
    s.tick += 1
    s.history = (s.history + [{"t": s.tick, "temp": round(t, 2)}])[-24:]

    state = s.get("chain_state")
    if state is None or s.get("chain_busy"):
        return
    if state["status_name"] not in TRACKING_STATUSES or state["settled"]:
        return

    temp_centi = int(round(t * 100))
    s.chain_busy = True
    try:
        receipt = send_tx(chain, chain["oracle"], chain["contract"].functions.recordTemperature(temp_centi))
        tx_info = {"hash": receipt.transactionHash.hex(), "block": receipt.blockNumber}
        s.tx_log["last_reading"] = tx_info

        started = chain["contract"].events.TemperatureViolationStarted().process_receipt(receipt)
        updated = chain["contract"].events.TemperatureViolationUpdated().process_receipt(receipt)
        resolved = chain["contract"].events.TemperatureViolationResolved().process_receipt(receipt)
        for ev in started:
            s.violation_tx_map.setdefault(ev["args"]["index"], {})["start"] = tx_info
        for ev in updated:
            s.violation_tx_map.setdefault(ev["args"]["index"], {})["latest"] = tx_info
        for ev in resolved:
            s.violation_tx_map.setdefault(ev["args"]["index"], {})["resolve"] = tx_info
        s.chain_read_error = None
    except Exception as e:
        s.chain_read_error = f"recordTemperature failed: {e}"
    finally:
        s.chain_busy = False


def guarded_tx(chain, account, fn, label, log_key=None):
    """Send one transaction with a session-state lock so a Streamlit rerun
    (button click or the auto-refreshing fragment) can never fire the same
    action twice while a previous one is still in flight."""
    s = st.session_state
    if s.get("chain_busy"):
        st.warning("Another transaction is already in progress \u2014 please wait.")
        return None
    s.chain_busy = True
    try:
        with st.spinner(f"Sending {label} transaction\u2026"):
            receipt = send_tx(chain, account, fn)
        info = {"hash": receipt.transactionHash.hex(), "block": receipt.blockNumber}
        if log_key:
            s.tx_log[log_key] = info
        return receipt
    except Exception as e:
        st.error(f"{label} failed: {e}")
        return None
    finally:
        s.chain_busy = False


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@st.fragment(run_every=TICK_SEC)
def main_app(chain):
    s = st.session_state

    try:
        state = fetch_full_state(chain)
        s.chain_state = state
        s.chain_read_error = None
    except Exception as e:
        st.error(f"\u26a0\ufe0f Lost connection to the blockchain: {e}")
        if st.button("\U0001f504 Retry connection"):
            get_chain.clear()
            st.rerun()
        return

    run_tick(chain)
    try:
        state = fetch_full_state(chain)
        s.chain_state = state
    except Exception:
        pass  # keep showing the pre-tick snapshot; next fragment cycle will retry

    deal_amount = state["deal_amount"]
    status_name = state["status_name"]
    state_label = STATUS_LABELS[status_name]
    terms_locked = state["status_int"] >= 4  # FundsLocked or later
    tracking_live = status_name in TRACKING_STATUSES and not state["settled"]

    current_temp = 27 + s.drift
    status_ok = state["active_idx"] == -1
    active_violation = None
    if state["active_idx"] != -1:
        for v in state["violations"]:
            if v["index"] == state["active_idx"]:
                active_violation = v
                break
    violation_duration = active_violation["durationSec"] if active_violation else 0
    preview_duration = violation_duration if active_violation else state["max_violation_duration"]
    current_percent, current_deduction, current_final = live_penalty_preview(chain, deal_amount, preview_duration)

    # ---- header ----
    h1, h2, h3 = st.columns([3, 2, 1])
    with h1:
        st.markdown(f"### Unit {state['container_id']}")
        st.caption(f"Condition: maintain {state['min_temp']:.1f}\u2013{state['max_temp']:.1f}\u00b0C while fan is active")
    with h2:
        badge(f"Agreement: {state_label}", state_label in ("TRACKING", "SETTLED", "FUNDS_LOCKED", "VIOLATION_RESOLVED"))
        if terms_locked:
            pill("\u2713 AGREEMENT ACTIVE", "teal")
            pill("\U0001f512 TERMS LOCKED", "amber")
    with h3:
        if st.button("\u21ba Reset demo", use_container_width=True):
            reset_demo()
            st.rerun()

    tab_agree, tab_live, tab_contract, tab_pay, tab_ledger, tab_invoice = st.tabs(
        ["Agreement", "Live tracking", "Smart contract", "Payment & settlement", "Blockchain ledger", "Invoice"]
    )

    # ======================================================================
    # AGREEMENT TAB
    # ======================================================================
    with tab_agree:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Shipper / product owner")
            s.shipper_name = st.text_input("Company name", value=s.shipper_name, disabled=terms_locked, key="in_shipper_name")
            s.shipper_email = st.text_input("Email", value=s.shipper_email, disabled=terms_locked, key="in_shipper_email")
            s.shipper_phone = st.text_input("Phone", value=s.shipper_phone, disabled=terms_locked, key="in_shipper_phone")
            s.shipper_address = st.text_input("Address", value=s.shipper_address, disabled=terms_locked, key="in_shipper_addr")
            st.text_input("Product ID (on-chain)", value=state["product_id"], disabled=True, key="in_product")
            st.text_input("Batch ID (on-chain)", value=state["batch_id"], disabled=True, key="in_batch")
        with c2:
            st.subheader("Transporter / logistics provider")
            s.transporter_name = st.text_input("Company name", value=s.transporter_name, disabled=terms_locked, key="in_transp_name")
            s.transporter_email = st.text_input("Email", value=s.transporter_email, disabled=terms_locked, key="in_transp_email")
            s.transporter_phone = st.text_input("Phone", value=s.transporter_phone, disabled=terms_locked, key="in_transp_phone")
            s.transporter_address = st.text_input("Address", value=s.transporter_address, disabled=terms_locked, key="in_transp_addr")
            st.text_input("Vehicle / container ID (on-chain)", value=state["container_id"], disabled=True, key="in_container")

        st.divider()
        st.subheader("Shipment conditions (immutable on-chain)")

        terms_editable = not state["shipper_signed"] and not state["transporter_signed"]

        c3, c4, c5 = st.columns(3)
        if terms_editable:
            if "edit_min_temp" not in s:
                s.edit_min_temp = state["min_temp"]
            if "edit_max_temp" not in s:
                s.edit_max_temp = state["max_temp"]
            if "edit_deal_amount" not in s:
                s.edit_deal_amount = float(deal_amount)
            with c3:
                s.edit_min_temp = st.number_input(
                    "Minimum temperature (\u00b0C)", value=s.edit_min_temp, key="in_edit_min_temp"
                )
            with c4:
                s.edit_max_temp = st.number_input(
                    "Maximum temperature (\u00b0C)", value=s.edit_max_temp, key="in_edit_max_temp"
                )
            with c5:
                s.edit_deal_amount = st.number_input(
                    "Original deal amount (\u20b9)", value=s.edit_deal_amount,
                    min_value=0.0, step=1.0, key="in_edit_deal_amount"
                )

            save_col, note_col = st.columns([1, 3])
            with save_col:
                if st.button("\U0001f4be Save shipment conditions", type="primary", disabled=s.chain_busy):
                    if s.edit_max_temp <= s.edit_min_temp:
                        st.error("Maximum temperature must be greater than minimum temperature.")
                    elif s.edit_deal_amount <= 0:
                        st.error("Deal amount must be greater than 0.")
                    else:
                        new_min = int(round(s.edit_min_temp * 100))
                        new_max = int(round(s.edit_max_temp * 100))
                        new_amount = int(round(s.edit_deal_amount))
                        receipt = guarded_tx(
                            chain, chain["shipper_acct"],
                            chain["contract"].functions.updateShipmentConditions(new_min, new_max, new_amount),
                            "Update shipment conditions", "update_conditions",
                        )
                        if receipt is not None:
                            for k in ("edit_min_temp", "edit_max_temp", "edit_deal_amount"):
                                s.pop(k, None)
                            st.rerun()
            with note_col:
                st.caption("Editable until either party signs \u2014 once signed, these are frozen on-chain, "
                           "exactly as shown above.")
        else:
            with c3:
                st.number_input("Minimum temperature (\u00b0C)", value=state["min_temp"], disabled=True, key="view_min_temp")
            with c4:
                st.number_input("Maximum temperature (\u00b0C)", value=state["max_temp"], disabled=True, key="view_max_temp")
            with c5:
                st.number_input("Original deal amount (\u20b9)", value=float(deal_amount), disabled=True, key="view_deal_amount")

        st.caption("Violation penalties \u2014 always applied to the ORIGINAL deal amount, never compounded:  "
                   "<1 min \u2192 0% \u00b7 1\u2013<2 min \u2192 10% \u00b7 2\u2013<5 min \u2192 25% \u00b7 5+ min \u2192 50% "
                   "(enforced on-chain by calculatePenalty()).")

        st.divider()
        st.markdown("##### Agreement preview")
        st.code(
            f"COLD-CHAIN SHIPMENT AGREEMENT (on-chain)\n"
            f"Agreement ID       : {state['agreement_id']}\n"
            f"Contract address   : {chain['address']}\n"
            f"Shipper address    : {state['shipper_addr']}\n"
            f"Transporter address: {state['transporter_addr']}\n"
            f"Oracle address     : {state['oracle_addr']}\n"
            f"Product / Batch    : {state['product_id']} / {state['batch_id']}\n"
            f"Container          : {state['container_id']}\n"
            f"Temperature rule   : {state['min_temp']:.1f}-{state['max_temp']:.1f} C\n"
            f"Original deal      : {inr(deal_amount)}",
            language="text",
        )

        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            if state["shipper_signed"]:
                pill("\u2713 SIGNED", "teal")
                if s.tx_log.get("shipper_sign"):
                    st.caption(f"tx {short(s.tx_log['shipper_sign']['hash'])} \u00b7 block #{s.tx_log['shipper_sign']['block']}")
            elif st.button("Sign as Shipper", type="primary", use_container_width=True, disabled=s.chain_busy):
                receipt = guarded_tx(chain, chain["shipper_acct"],
                                      chain["contract"].functions.signAgreement(),
                                      "Shipper signature", "shipper_sign")
                if receipt is not None:
                    st.rerun()
        with sc2:
            if state["transporter_signed"]:
                pill("\u2713 SIGNED", "teal")
                if s.tx_log.get("transporter_sign"):
                    st.caption(f"tx {short(s.tx_log['transporter_sign']['hash'])} \u00b7 block #{s.tx_log['transporter_sign']['block']}")
            elif st.button("Sign as Transporter", type="primary", use_container_width=True, disabled=s.chain_busy):
                receipt = guarded_tx(chain, chain["transporter_acct"],
                                      chain["contract"].functions.signAgreement(),
                                      "Transporter signature", "transporter_sign")
                if receipt is not None:
                    st.rerun()
        with sc3:
            chain_tag("REAL ON-CHAIN SIGNATURE (Hardhat account, ECDSA)")

        st.write("")
        if status_name in ("Draft", "WaitingForShipperSignature", "WaitingForTransporterSignature"):
            pill("WAITING FOR SECOND PARTY" if state["shipper_signed"] or state["transporter_signed"] else "WAITING FOR SIGNATURES", "amber")
        elif status_name == "Signed":
            pill("\u2713 AGREEMENT SIGNED", "teal")
            if st.button("\U0001f512 Activate contract & lock funds", type="primary", disabled=s.chain_busy):
                # activateAgreement() has no sender restriction on-chain (any account may
                # call it once both signatures are in) — the backend/oracle account sends it.
                receipt = guarded_tx(chain, chain["oracle"],
                                      chain["contract"].functions.activateAgreement(),
                                      "Activate agreement", "activate")
                if receipt is not None:
                    st.rerun()
        elif terms_locked:
            pill("\u2713 AGREEMENT SIGNED", "teal")
            pill("\u2713 CONTRACT ACTIVE", "teal")
            pill("\U0001f512 FUNDS LOCKED (SYMBOLIC \u2014 SEE CONTRACT NOTE)", "amber")

        if status_name == "FundsLocked":
            st.write("")
            if st.button("\u25cf Start tracking", type="primary", disabled=s.chain_busy):
                receipt = guarded_tx(chain, chain["shipper_acct"],
                                      chain["contract"].functions.startTracking(),
                                      "Start tracking", "start_tracking")
                if receipt is not None:
                    st.rerun()
        elif tracking_live:
            badge("\u25cf IoT monitoring active", True)

    # ======================================================================
    # LIVE TRACKING TAB
    # ======================================================================
    with tab_live:
        if not tracking_live:
            st.info("Sign the agreement, activate the contract, and click **Start tracking** on the Agreement tab first.")
        else:
            c1, c2 = st.columns([1, 2])
            with c1:
                dial(current_temp, status_ok)
                st.caption(f"Target range: {state['min_temp']:.1f}\u2013{state['max_temp']:.1f}\u00b0C")
                fan_label = f"Fan {'ON' if s.fan_on else 'OFF'} \u2014 tap to toggle"
                if st.button(fan_label, use_container_width=True, type="primary" if s.fan_on else "secondary"):
                    s.fan_on = not s.fan_on
                    st.rerun()
                st.metric("Violation duration", format_duration(violation_duration))
                if not status_ok:
                    badge(f"\u26a0 TEMPERATURE VIOLATION \u00b7 {format_duration(violation_duration)}", False)
                st.caption("Turn the fan off to break the condition live \u2014 a violation is judged on-chain automatically.")
            with c2:
                fig = go.Figure()
                fig.add_hrect(y0=state["min_temp"], y1=state["max_temp"], fillcolor=COLORS["teal"], opacity=0.08, line_width=0)
                xs = [p["t"] for p in s.history]
                ys = [p["temp"] for p in s.history]
                fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines",
                                          line=dict(color=COLORS["teal"] if status_ok else COLORS["coral"], width=2)))
                fig.update_layout(
                    template="plotly_dark", height=340, margin=dict(l=10, r=10, t=30, b=10),
                    paper_bgcolor=COLORS["surface"], plot_bgcolor=COLORS["surface"],
                    xaxis=dict(visible=False),
                    yaxis=dict(range=[state["min_temp"] - 6, state["max_temp"] + 6], gridcolor=COLORS["border"]),
                    showlegend=False,
                )
                st.plotly_chart(fig, use_container_width=True)

            if current_percent >= 50:
                tier_note = "Penalty tier: 50% (5+ min)"
            elif current_percent >= 25:
                tier_note = "Penalty tier: 25% (2\u2013<5 min)"
            elif current_percent >= 10:
                tier_note = "Penalty tier: 10% (1\u2013<2 min)"
            else:
                tier_note = "Penalty tier: 0% (<1 min)"
            st.caption(f"{tier_note} (from calculatePenalty() on-chain) \u00b7 Projected deduction {inr(current_deduction)} \u00b7 "
                       f"Projected final payment {inr(current_final)}")
            if s.tx_log.get("last_reading"):
                st.caption(f"Last reading tx: {short(s.tx_log['last_reading']['hash'])} \u00b7 "
                           f"block #{s.tx_log['last_reading']['block']} \u00b7 "
                           f"{state['reading_count']} readings recorded on-chain")
            if s.chain_read_error:
                st.error(s.chain_read_error)

    # ======================================================================
    # SMART CONTRACT TAB
    # ======================================================================
    with tab_contract:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Contract identity")
            rows = [
                ("Agreement ID", state["agreement_id"]),
                ("Contract address", chain["address"]),
                ("Shipper", f"{s.shipper_name}"),
                ("Shipper address", state["shipper_addr"]),
                ("Transporter", f"{s.transporter_name}"),
                ("Transporter address", state["transporter_addr"]),
                ("Authorized oracle", state["oracle_addr"]),
                ("Agreement status", f"{state_label} (Solidity enum #{state['status_int']})"),
                ("Shipper signed", "\u2713 yes" if state["shipper_signed"] else "\u2014 no"),
                ("Transporter signed", "\u2713 yes" if state["transporter_signed"] else "\u2014 no"),
            ]
            for k, v in rows:
                a, b = st.columns([2, 1])
                a.caption(k)
                b.write(f"**{v}**")
        with c2:
            st.subheader("Live contract state")
            rows2 = [
                ("Deal amount (symbolic)", inr(deal_amount) if terms_locked else f"{inr(deal_amount)} \u2014 not activated"),
                ("Tracking status", "\u25cf active" if tracking_live else "not started"),
                ("Current violation", format_duration(violation_duration) if active_violation else "none"),
                ("Current penalty tier", f"{current_percent}%"),
                ("Total readings recorded", state["reading_count"]),
                ("Total violations logged", state["violation_count"]),
            ]
            for k, v in rows2:
                a, b = st.columns([2, 1])
                a.caption(k)
                b.write(f"**{v}**")
            chain_tag(f"LIVE ON-CHAIN \u00b7 chainId {chain['w3'].eth.chain_id} \u00b7 {RPC_URL}")

    # ======================================================================
    # PAYMENT & SETTLEMENT TAB
    # ======================================================================
    with tab_pay:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Settlement")
            rows = [
                ("Original deal amount", inr(deal_amount)),
                ("Locked amount", inr(deal_amount) if terms_locked else "\u2014"),
                ("Current penalty", f"{current_percent}%"),
                ("Current deduction", inr(current_deduction)),
                ("Projected final payment", inr(current_final)),
            ]
            for k, v in rows:
                a, b = st.columns([2, 1])
                a.caption(k)
                b.write(f"**{v}**")
            can_settle = (status_name in ("Active", "ViolationResolved")
                          and state["active_idx"] == -1 and not state["settled"])
            if st.button("\U0001f6e1\ufe0f Complete shipment & settle", type="primary",
                         use_container_width=True, disabled=not can_settle or s.chain_busy):
                receipt = guarded_tx(chain, chain["oracle"],
                                      chain["contract"].functions.completeShipment(),
                                      "Complete shipment", "complete")
                if receipt is not None:
                    st.rerun()
            if state["settled"]:
                st.success("Shipment settled \u2014 see the Invoice tab.")
        with c2:
            st.subheader("Live payment preview")
            m1, m2 = st.columns(2)
            m1.metric("Original deal", inr(deal_amount))
            m2.metric("Violation duration", format_duration(violation_duration))
            m3, m4 = st.columns(2)
            m3.metric("Current penalty", f"{current_percent}%")
            m4.metric("Amount deducted", inr(current_deduction))
            st.metric("Projected final payment", inr(current_final))
            st.caption("Tiers: 0\u2013<1 min \u2192 0% \u00b7 1\u2013<2 min \u2192 10% \u00b7 2\u2013<5 min \u2192 25% \u00b7 5+ min \u2192 50%, "
                       "always applied to the original deal amount \u2014 enforced by the contract, read live above.")

    # ======================================================================
    # BLOCKCHAIN LEDGER TAB
    # ======================================================================
    with tab_ledger:
        if not state["violations"]:
            st.info("No violations yet \u2014 turn the fan off on Live tracking to trigger one.")
        else:
            st.caption(f"{len(state['violations'])} record{'s' if len(state['violations']) != 1 else ''} "
                       f"for agreement {state['agreement_id']} \u2014 read live from the contract")
            chain_tag("LIVE BLOCKCHAIN LEDGER \u2014 real events, real receipts")
            for v in state["violations"]:
                idx = v["index"]
                is_active = idx == state["active_idx"]
                if v["resolved"]:
                    live_percent, live_deducted, live_final = v["penaltyPercent"], v["deductedAmount"], deal_amount - v["deductedAmount"]
                else:
                    live_percent, live_deducted, live_final = live_penalty_preview(chain, deal_amount, v["durationSec"])
                tx_info = s.violation_tx_map.get(idx, {})
                shown_tx = tx_info.get("resolve") or tx_info.get("latest") or tx_info.get("start")
                with st.container(border=True):
                    top1, top2 = st.columns([3, 1])
                    top1.markdown(f"**VIOLATION #{idx + 1:03d}**")
                    with top2:
                        pill("ACTIVE" if is_active else "VERIFIED", "amber" if is_active else "teal")
                    g1, g2, g3 = st.columns(3)
                    g1.caption(f"Start: {datetime.fromtimestamp(v['startTime']).strftime('%H:%M:%S')}")
                    g2.caption(f"End: {datetime.fromtimestamp(v['endTime']).strftime('%H:%M:%S') if v['endTime'] else '\u2014'}")
                    g3.caption(f"Duration: {format_duration(v['durationSec'])}")
                    g4, g5, g6 = st.columns(3)
                    g4.caption(f"Temp at start: {v['tempAtStartCentiC'] / 100.0:.1f}\u00b0C")
                    g5.caption(f"Min/Max: {state['min_temp']:.1f}\u00b0C / {state['max_temp']:.1f}\u00b0C")
                    g6.caption(f"Allowed: {state['min_temp']:.1f}\u2013{state['max_temp']:.1f}\u00b0C")
                    g7, g8, g9 = st.columns(3)
                    g7.caption(f"Penalty: {live_percent}%")
                    g8.caption(f"Deducted: {inr(live_deducted)}")
                    g9.caption(f"Final payment: {inr(live_final)}")
                    if shown_tx:
                        st.code(f"hash {shown_tx['hash']} \u00b7 block #{shown_tx['block']}", language="text")
                    else:
                        st.code("tx pending confirmation\u2026", language="text")

    # ======================================================================
    # INVOICE TAB
    # ======================================================================
    with tab_invoice:
        if not state["settled"]:
            st.info("No settlement yet. Go to **Payment & settlement** and click **Complete shipment & settle**.")
        else:
            reason = ("No temperature excursions \u2014 shipment stayed within range for its entire journey."
                       if state["final_pct"] == 0 else
                       f"Worst temperature excursion lasted {format_duration(state['max_violation_duration'])}, "
                       f"outside the {state['min_temp']:.1f}\u2013{state['max_temp']:.1f}\u00b0C contractual range.")
            st.subheader("COLD-CHAIN SETTLEMENT INVOICE")
            complete_tx = s.tx_log.get("complete")
            if complete_tx:
                st.caption(f"Settled in block #{complete_tx['block']}")
            rows = [
                ("Agreement ID", state["agreement_id"]),
                ("Shipment / container", state["container_id"]),
                ("Product / Batch", f"{state['product_id']} / {state['batch_id']}"),
                ("Shipper", s.shipper_name),
                ("Transporter", s.transporter_name),
                ("Original deal", inr(deal_amount)),
                ("Temperature rule", f"{state['min_temp']:.1f}\u2013{state['max_temp']:.1f}\u00b0C"),
                ("Maximum violation", format_duration(state["max_violation_duration"])),
                ("Penalty", f"{state['final_pct']}%"),
                ("Deducted amount", inr(state["deducted_amt"])),
                ("Final payment", inr(state["final_amt"])),
            ]
            for k, v in rows:
                a, b = st.columns([2, 1])
                a.caption(k)
                b.write(f"**{v}**")
            st.markdown(f"**Reason:** {reason}")
            chain_tag("REAL ON-CHAIN SETTLEMENT \u2014 from getSettlementSummary()")
            if complete_tx:
                st.code(f"Contract address: {chain['address']}\n"
                        f"Settlement tx: {complete_tx['hash']}\n"
                        f"Block: {complete_tx['block']}", language="text")
            pill("SETTLEMENT CALCULATED" if state["final_pct"] > 0 else "\u2713 PAID IN FULL",
                 "amber" if state["final_pct"] > 0 else "teal")
            pill("\u2713 AGREEMENT STATUS: SETTLED", "teal")


# ---------------------------------------------------------------------------
init_state()
_chain = get_chain()
if not _chain["ok"]:
    st.error("\u26a0\ufe0f Cannot start the dashboard \u2014 blockchain backend is not reachable:")
    for e in _chain["errors"]:
        st.markdown(f"- {e}")
    st.caption("Fix the issue above (start `npx hardhat node`, deploy the contract, "
               "and/or fill in .env), then retry.")
    if st.button("\U0001f504 Retry connection"):
        get_chain.clear()
        st.rerun()
else:
    main_app(_chain)
