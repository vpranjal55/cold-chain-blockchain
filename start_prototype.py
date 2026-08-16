"""
Cold-Chain Prototype — TRUE ONE-CLICK LAUNCHER
================================================
Run with:   python start_prototype.py
(or just double-click it if .py files are associated with Python)

What this does, in order, with zero manual terminals:
  1. Checks Node.js/npm and the required Python packages are installed,
     and runs `npm install` once automatically if node_modules is missing.
  2. Starts a local Hardhat blockchain node (npx hardhat node) in the
     background and waits until its JSON-RPC endpoint actually answers
     (not just "port is open").
  3. Deploys ColdChainAgreement.sol fresh via scripts/deploy.js, which
     writes contract_abi.json and deployment.json at the project root —
     scripts/deploy.js itself is not touched or duplicated here.
  4. Reads the freshly deployed contract address out of deployment.json
     and injects it into the child process environment as
     CONTRACT_ADDRESS. app.py picks this up automatically: python-dotenv's
     load_dotenv() never overrides a variable that is already set in the
     process environment, so this freshly-deployed address always wins
     over whatever stale value is sitting in .env. contract_abi.json is
     already written to the project root by deploy.js, which is exactly
     where app.py looks for it — no copying needed.
  5. Starts app.py as a headless Streamlit server and waits until it is
     actually responding.
  6. Opens it in a native desktop window (pywebview) — no browser bar,
     no address bar, no Streamlit "Deploy" menu.
  7. On window close, Ctrl+C, or any startup failure at any step, cleanly
     tears down both the Streamlit process and the Hardhat node,
     including their full child-process trees (important on Windows,
     where `npx` spawns node.exe as a child of a cmd wrapper).

Nothing here modifies contracts/ColdChainAgreement.sol, scripts/deploy.js,
app.py, or hardhat.config.js — this file only orchestrates them.
Existing functionality (signing, activation, tracking, violation
detection/resolution, completion, settlement, invoice) is all still
served by the untouched app.py talking to the untouched contract.

One-time setup before first run:
    pip install -r requirements.txt
    pip install pywebview
"""

import atexit
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
APP_FILE = PROJECT_DIR / "app.py"
DEPLOY_SCRIPT = "scripts/deploy.js"
DEPLOYMENT_JSON = PROJECT_DIR / "deployment.json"
ABI_JSON = PROJECT_DIR / "contract_abi.json"

HARDHAT_HOST = "127.0.0.1"
HARDHAT_PORT = 8545
RPC_URL = f"http://{HARDHAT_HOST}:{HARDHAT_PORT}"

STREAMLIT_HOST = "127.0.0.1"
STREAMLIT_PORT = 8501

WINDOW_TITLE = "Cold-Chain Smart Contract"
IS_WINDOWS = platform.system() == "Windows"

HARDHAT_STARTUP_TIMEOUT = 60
DEPLOY_TIMEOUT = 120
STREAMLIT_STARTUP_TIMEOUT = 45

_hardhat_proc = None
_streamlit_proc = None
_shutting_down = False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[launcher] {msg}", flush=True)


def fail(msg):
    log(f"ERROR: {msg}")
    shutdown()
    sys.exit(1)


def find_exe(name):
    """Resolve an executable across platforms. On Windows, npm/npx are
    .cmd shims, which shutil.which() only finds if PATHEXT is checked —
    it is, by default."""
    return shutil.which(name)


def node_cmd(exe_path, *args):
    """Build a command list that actually runs on Windows. npx/npm on
    Windows are .cmd batch files, and Python's subprocess cannot execute
    a .cmd/.bat directly via CreateProcess without shell involvement, so
    on Windows we route through `cmd /c`. On macOS/Linux, npx is a real
    executable and needs no wrapping."""
    if IS_WINDOWS and exe_path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe_path, *args]
    return [exe_path, *args]


def _port_open(host, port, timeout=0.5):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            return s.connect_ex((host, port)) == 0
        except OSError:
            return False


def _rpc_ready(url, timeout=0.5):
    """A real JSON-RPC round trip, not just an open port — Hardhat's port
    can start accepting connections slightly before it will answer calls."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return "result" in data
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError):
        return False


def _stream_output(proc, prefix):
    def _pump():
        try:
            for line in proc.stdout:
                print(f"[{prefix}] {line.rstrip()}", flush=True)
        except (ValueError, OSError):
            pass  # pipe closed during shutdown — nothing to do

    threading.Thread(target=_pump, daemon=True).start()


def _popen_background(cmd, cwd, log_prefix, env=None):
    """Start a background process in its own process group/session so the
    whole tree (npx -> node, on both Windows and POSIX) can be killed
    later instead of leaving orphaned processes running after the demo."""
    kwargs = dict(
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        bufsize=1,
    )
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["preexec_fn"] = os.setsid
    proc = subprocess.Popen(cmd, **kwargs)
    _stream_output(proc, log_prefix)
    return proc


def _kill_tree(proc, name):
    if proc is None or proc.poll() is not None:
        return
    log(f"Stopping {name} (pid {proc.pid})...")
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as e:
        log(f"  ({name} already gone: {e})")


def shutdown():
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    _kill_tree(_streamlit_proc, "Streamlit")
    _kill_tree(_hardhat_proc, "Hardhat node")


atexit.register(shutdown)


def _signal_handler(signum, frame):
    log(f"Received signal {signum}, shutting down...")
    shutdown()
    sys.exit(1)


signal.signal(signal.SIGINT, _signal_handler)
if not IS_WINDOWS:
    signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
def check_prereqs():
    log("Checking prerequisites...")

    if not APP_FILE.exists():
        fail(f"{APP_FILE} not found. This launcher must sit in the project root, next to app.py.")

    npx = find_exe("npx")
    if not npx:
        fail("Node.js/npx not found on PATH. Install Node.js from https://nodejs.org "
             "(the LTS version), restart your terminal, and re-run.")

    if not (PROJECT_DIR / "contracts" / "ColdChainAgreement.sol").exists():
        fail("contracts/ColdChainAgreement.sol not found — run this from the project root.")

    if not (PROJECT_DIR / "node_modules").exists():
        npm = find_exe("npm")
        if not npm:
            fail("node_modules is missing and npm was not found on PATH to install it.")
        log("node_modules missing — running `npm install` once (this can take a minute)...")
        result = subprocess.run(node_cmd(npm, "install"), cwd=str(PROJECT_DIR))
        if result.returncode != 0:
            fail("npm install failed. Check your internet connection and try again.")
        log("npm install complete.")

    try:
        import webview  # noqa: F401
    except ImportError:
        fail("The 'pywebview' package is not installed. Run: pip install pywebview")

    for pkg in ("streamlit", "web3", "dotenv", "plotly"):
        try:
            __import__(pkg)
        except ImportError:
            fail(f"Missing Python package '{pkg}'. Run: pip install -r requirements.txt")

    if _port_open(HARDHAT_HOST, HARDHAT_PORT):
        fail(f"Port {HARDHAT_PORT} is already in use — is a Hardhat node already running "
             f"in another window? Close it first; this launcher starts its own.")
    if _port_open(STREAMLIT_HOST, STREAMLIT_PORT):
        fail(f"Port {STREAMLIT_PORT} is already in use — close whatever is using it and re-run.")

    log("Prerequisites OK.")
    return npx


# ---------------------------------------------------------------------------
# Step 1: Hardhat node
# ---------------------------------------------------------------------------
def start_hardhat_node(npx):
    global _hardhat_proc
    log("Starting local Hardhat blockchain node...")
    _hardhat_proc = _popen_background(
        node_cmd(npx, "hardhat", "node"), cwd=PROJECT_DIR, log_prefix="hardhat-node"
    )

    deadline = time.time() + HARDHAT_STARTUP_TIMEOUT
    while time.time() < deadline:
        if _hardhat_proc.poll() is not None:
            fail("Hardhat node exited unexpectedly during startup — see [hardhat-node] output above.")
        if _rpc_ready(RPC_URL):
            log(f"Hardhat node is up at {RPC_URL} (chainId 31337).")
            return
        time.sleep(0.4)
    fail(f"Hardhat node did not respond at {RPC_URL} within {HARDHAT_STARTUP_TIMEOUT}s.")


# ---------------------------------------------------------------------------
# Step 2: Deploy contract fresh
# ---------------------------------------------------------------------------
def deploy_contract(npx):
    log("Deploying ColdChainAgreement.sol to the local node...")
    try:
        result = subprocess.run(
            node_cmd(npx, "hardhat", "run", DEPLOY_SCRIPT, "--network", "localhost"),
            cwd=str(PROJECT_DIR), timeout=DEPLOY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        fail(f"Contract deployment did not finish within {DEPLOY_TIMEOUT}s.")
    if result.returncode != 0:
        fail("Contract deployment failed — see the Hardhat output above for the Solidity/compile error.")

    if not DEPLOYMENT_JSON.exists() or not ABI_JSON.exists():
        fail("Deploy script did not produce deployment.json / contract_abi.json as expected.")

    info = json.loads(DEPLOYMENT_JSON.read_text())
    address = info.get("contractAddress")
    if not address:
        fail("deployment.json has no contractAddress field.")
    log(f"Contract deployed at {address}.")
    return address


# ---------------------------------------------------------------------------
# Step 3: Streamlit backend, pointed at the fresh deployment
# ---------------------------------------------------------------------------
def start_streamlit(fresh_contract_address):
    global _streamlit_proc
    log("Starting the dashboard backend...")

    env = os.environ.copy()
    # This override — not an edit to .env — is what makes "always use the
    # freshly deployed address" safe on every run: app.py's load_dotenv()
    # never overrides a variable already present in the process
    # environment, so this always wins over whatever address is in .env.
    env["CONTRACT_ADDRESS"] = fresh_contract_address

    cmd = [
        sys.executable, "-m", "streamlit", "run", str(APP_FILE),
        "--server.headless=true",
        f"--server.port={STREAMLIT_PORT}",
        f"--server.address={STREAMLIT_HOST}",
        "--browser.gatherUsageStats=false",
        "--client.toolbarMode=minimal",
        "--client.showSidebarNavigation=false",
    ]
    _streamlit_proc = _popen_background(cmd, cwd=PROJECT_DIR, log_prefix="streamlit", env=env)

    deadline = time.time() + STREAMLIT_STARTUP_TIMEOUT
    while time.time() < deadline:
        if _streamlit_proc.poll() is not None:
            fail("Streamlit exited unexpectedly during startup — see [streamlit] output above.")
        if _port_open(STREAMLIT_HOST, STREAMLIT_PORT):
            log("Dashboard backend is up.")
            return
        time.sleep(0.3)
    fail(f"Streamlit did not come up within {STREAMLIT_STARTUP_TIMEOUT}s.")


# ---------------------------------------------------------------------------
# Step 4: Desktop window
# ---------------------------------------------------------------------------
def open_window():
    import webview
    log("Opening desktop window...")
    webview.create_window(
        WINDOW_TITLE,
        url=f"http://{STREAMLIT_HOST}:{STREAMLIT_PORT}",
        width=1440,
        height=900,
        min_size=(1024, 700),
    )
    webview.start()  # blocks until the window is closed


# ---------------------------------------------------------------------------
def main():
    log("Cold-Chain Prototype — one-click startup")
    npx = check_prereqs()
    start_hardhat_node(npx)
    address = deploy_contract(npx)
    start_streamlit(address)
    open_window()
    log("Window closed — shutting down.")
    shutdown()


if __name__ == "__main__":
    main()
