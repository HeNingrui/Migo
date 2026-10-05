"""Portable bootstrap. No reset, no keys needed, only loopback HTTP."""
import argparse
import importlib.metadata
import os
import json
import socket
from pathlib import Path
import subprocess
import sys
import threading
import webbrowser
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description="Start the complete simulated headphone agent")
    parser.add_argument("--port",type=int,default=8768)
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--offline",action="store_true",help="Force local rule parsing")
    args=parser.parse_args()
    if sys.version_info < (3,12):
        print("Python 3.12 or newer is required. Install Python, then run Start.cmd again.")
        return 1
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    url=f"http://127.0.0.1:{args.port}/"
    try:
        with socket.create_connection(("127.0.0.1",args.port),timeout=1):
            occupied=True
    except OSError:
        occupied=False
    if occupied:
        try:
            with urllib.request.urlopen(url+"api/v1/health",timeout=2) as response:
                health=json.load(response)
            data=health.get("data") or {}
            ours=(health.get("ok") is True and
                  str(data.get("search","")).startswith("integrated B") and
                  data.get("commerce",{}).get("settlement_source_type")=="SANDBOX")
        except (OSError,ValueError):
            ours=False
        if ours:
            print(f"This project is already running. Open {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print(f"Port {args.port} is occupied. Run Start.cmd --port {args.port+1} with a free port.")
        return 1
    runtime=ROOT / ".venv" / ("Scripts/python.exe" if os.name=="nt" else "bin/python")
    if not runtime.is_file():
        print("Creating the project's local Python environment...")
        subprocess.run([sys.executable,"-m","venv",str(ROOT/".venv")],check=True)
    requirements=ROOT / "requirements.lock.txt"
    probe="import importlib.metadata as m; from pathlib import Path; import sys; pins=[l.strip().split('==') for l in Path(sys.argv[1]).read_text().splitlines() if '==' in l and not l.startswith('#')]; sys.exit(0 if all(m.version(n)==v for n,v in pins) else 1)"
    installed=subprocess.run([str(runtime),"-c",probe,str(requirements)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if not installed:
        print("Installing the team's locked dependencies (internet needed on first launch)...")
        subprocess.run([str(runtime),"-m","pip","install","-r",str(requirements)],check=True)
    env=dict(os.environ)
    if args.offline:
        env["LLM_ENABLED"]="false"
    print(f"Simulation only. Open {url} . Stop with Ctrl+C. Existing orders and balances are preserved.")
    if not args.no_browser:
        opener=threading.Timer(3,lambda:webbrowser.open(url))
        opener.daemon=True
        opener.start()
    try:
        return subprocess.call([str(runtime),"-m","uvicorn","app.main:app","--host","127.0.0.1","--port",str(args.port)],cwd=ROOT,env=env)
    except KeyboardInterrupt:
        return 0


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except (OSError,subprocess.CalledProcessError) as exc:
        print(f"Startup failed: {exc}",file=sys.stderr)
        raise SystemExit(1)
