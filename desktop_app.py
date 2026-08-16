"""
Cold-Chain Dashboard — Desktop launcher
=======================================
Run with:   python desktop_app.py

What this does, in order:
1. Starts your existing Streamlit app (app.py) as a background subprocess,
   headless, with the Streamlit menu/toolbar hidden so it looks like a
   finished product instead of a dev server.
2. Waits until the local Streamlit server is actually answering requests
   (not just "process started") before opening the window, so judges never
   see a blank/connection-refused page.
3. Opens a native OS window (pywebview) pointed at that local server, with
   no address bar and no browser chrome.
4. On window close, kills the Streamlit subprocess cleanly.

This does NOT touch app.py, the Hardhat node, or the deployed contract.
Start `npx hardhat node` and deploy your contract exactly as before — this
just changes how the UI is presented.

Install once:
    pip install pywebview

Windows-only note: pywebview uses the system WebView2 runtime, which ships
with Windows 10/11 by default. On Linux it uses GTK/QT WebKit — install
`pip install pywebview[gtk]` if the window fails to open. On macOS it uses
the built-in WebKit, no extra install needed.
"""

import atexit
import socket
import subprocess
import sys
import time
from pathlib import Path

import webview

APP_DIR = Path(__file__).resolve().parent
APP_FILE = APP_DIR / "app.py"          # change this if your file is named differently (e.g. app2.py)
HOST = "127.0.0.1"
PORT = 8501                             # Streamlit's default port
WINDOW_TITLE = "Cold-Chain Smart Contract"

_streamlit_proc = None


def _port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            return s.connect_ex((host, port)) == 0
        except OSError:
            return False


def start_streamlit():
    global _streamlit_proc
    if not APP_FILE.exists():
        print(f"ERROR: {APP_FILE} not found. Edit APP_FILE in this script if your "
              f"Streamlit file has a different name.")
        sys.exit(1)

    cmd = [
        sys.executable, "-m", "streamlit", "run", str(APP_FILE),
        "--server.headless=true",
        f"--server.port={PORT}",
        "--server.address=" + HOST,
        "--browser.gatherUsageStats=false",
        "--client.toolbarMode=minimal",   # hides the Streamlit "Deploy"/menu chrome
        "--client.showSidebarNavigation=false",
    ]
    _streamlit_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(APP_DIR),
    )
    atexit.register(stop_streamlit)


def stop_streamlit():
    global _streamlit_proc
    if _streamlit_proc and _streamlit_proc.poll() is None:
        _streamlit_proc.terminate()
        try:
            _streamlit_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _streamlit_proc.kill()


def wait_for_server(host: str, port: int, timeout_sec: float = 30.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _port_is_open(host, port):
            return True
        if _streamlit_proc.poll() is not None:
            # Streamlit process died — surface its output instead of hanging
            out = _streamlit_proc.stdout.read().decode(errors="replace") if _streamlit_proc.stdout else ""
            print("Streamlit exited early:\n", out)
            return False
        time.sleep(0.3)
    return False


def main():
    print("Starting Streamlit backend...")
    start_streamlit()

    if not wait_for_server(HOST, PORT):
        print("ERROR: Streamlit server never came up. Check the error output above, "
              "and make sure the Hardhat node + deployed contract are already running "
              "if app.py expects them at startup.")
        stop_streamlit()
        sys.exit(1)

    print("Backend is up — opening desktop window.")
    webview.create_window(
        WINDOW_TITLE,
        url=f"http://{HOST}:{PORT}",
        width=1440,
        height=900,
        min_size=(1024, 700),
    )
    webview.start()   # blocks until the window is closed
    stop_streamlit()


if __name__ == "__main__":
    main()
