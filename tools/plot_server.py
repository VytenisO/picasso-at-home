#!/usr/bin/env python3
"""Web UI for the WaterColorBot pen plotter (stdlib only; Pillow for image conversion).

    python3 tools/plot_server.py                       bind the Tailscale IP, port from config/web.json (8080)
    python3 tools/plot_server.py --sim --host 127.0.0.1 --port 8080
                                                       simulator: every job runs plot_svg.py --sim, the serial
                                                       port is never opened
Options can also come from the environment: PLOTTER_WEB_HOST, PLOTTER_WEB_PORT, PLOTTER_WEB_PASSWORD,
PLOTTER_WEB_AUTH=off, PLOTTER_WEB_SIM=1. The systemd unit is config/plotter-web.service.

The server is a thin layer over tools/plot_svg.py: every motion goes through it, with its own checks, after a
preview the operator saw and a confirmation that the carriage is parked. Design and API: docs/ui-design.md.
"""
import argparse
import os
import pathlib
import signal
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from plotweb.api import App, Server, shutdown_job  # noqa: E402
from plotweb.auth import load_web_config  # noqa: E402
from plotweb.config import Settings  # noqa: E402


def detect_host():
    try:
        out = subprocess.check_output(["tailscale", "ip", "-4"], text=True, timeout=5)
        return out.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "127.0.0.1"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("PLOTTER_WEB_HOST"), help="bind address (default: tailscale IP)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PLOTTER_WEB_PORT", 0)) or None)
    ap.add_argument("--sim", action="store_true", default=os.environ.get("PLOTTER_WEB_SIM", "") in ("1", "on", "yes"),
                    help="simulator: no hardware is ever touched")
    ap.add_argument("--sim-speed", type=float, default=20.0, help="real-time factor of simulated runs (default 20)")
    ap.add_argument("--data", help="directory holding input/ and results/ (default: the repo)")
    ap.add_argument("--machine", help="machine config (default: config/machine.json)")
    ap.add_argument("--pens", help="pen profiles (default: config/pens.json)")
    ap.add_argument("--web-config", help="auth/secret file (default: config/web.json)")
    a = ap.parse_args(argv)
    if not 0 < a.sim_speed <= 1000:
        sys.exit("--sim-speed must be in (0, 1000]")

    s = Settings(a.data, a.machine, a.pens, a.web_config, sim=a.sim, sim_speed=a.sim_speed)
    cfg, password, source = load_web_config(s.web_config)
    app = App(s, cfg)
    host = a.host or detect_host()
    port = a.port or int(cfg.get("port", 8080))
    srv = Server((host, port), app)

    def on_term(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, on_term)
    print(f"Plotter web UI -> http://{host}:{srv.server_address[1]}/" + ("   [SIMULATOR]" if s.sim else ""))
    if not cfg.get("auth", True):
        print("Authentication DISABLED (PLOTTER_WEB_AUTH=off). Anyone who can reach this address can use it.")
    elif password:
        print(f"Password ({source}): {password}")
    print("Press Ctrl-C to stop.", flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopping: any running job is stopped first (pen up, motors off)", flush=True)
    finally:
        srv.stopping = True
        shutdown_job(app)
        srv.server_close()


if __name__ == "__main__":
    main()
