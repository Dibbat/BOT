#!/usr/bin/env python3
"""
Demo live-trading launcher for the MT5 bridge.

This script wraps pt_mt5_bridge with:
- demo-server safety check
- MT5 authorization retries
- optional terminal auto-start/warmup

Usage:
  python mt5/pt_mt5_demo_live.py --config mt5/mt5_config.json
"""

import argparse
import os
import subprocess
import time

import pt_mt5_bridge as bridge


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start MT5 bridge in demo live mode with auth retry"
    )
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "mt5_config.json"),
        help="Path to MT5 JSON config file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one reconciliation cycle and exit",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Number of MT5 authorization attempts",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=8.0,
        help="Seconds to wait between auth attempts",
    )
    parser.add_argument(
        "--terminal-warmup",
        type=float,
        default=10.0,
        help="Seconds to wait after launching terminal",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode (no live orders)",
    )
    parser.add_argument(
        "--allow-non-demo",
        action="store_true",
        help="Allow non-demo MT5 servers (safety bypass)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=0,
        help="Override poll_seconds from config (0 keeps config value)",
    )
    return parser.parse_args()


def _start_terminal(terminal_path: str, warmup_seconds: float) -> None:
    if not terminal_path:
        bridge.log("[WARN] terminal_path is empty; skipping terminal auto-start")
        return

    if not os.path.isfile(terminal_path):
        bridge.log(f"[WARN] terminal_path not found: {terminal_path}")
        return

    bridge.log(f"[AUTH] Launching MT5 terminal: {terminal_path}")
    try:
        subprocess.Popen([terminal_path])
    except Exception as e:
        bridge.log(f"[WARN] Failed to launch terminal: {e}")
        return

    if warmup_seconds > 0:
        time.sleep(warmup_seconds)


def _authorize_with_retry(config: dict, retries: int, retry_delay: float, terminal_warmup: float) -> bool:
    retries = max(1, int(retries))
    retry_delay = max(0.0, float(retry_delay))

    for attempt in range(1, retries + 1):
        bridge.log(f"[AUTH] Attempt {attempt}/{retries}")
        _start_terminal(str(config.get("terminal_path", "")).strip(), terminal_warmup)

        try:
            bridge.initialize_mt5(config)
            bridge.log("[AUTH] MT5 authorization succeeded")
            return True
        except Exception as e:
            bridge.log(f"[WARN] {e}")
            bridge.log(f"[WARN] MT5 last_error: {bridge.mt5.last_error()}")
            try:
                bridge.mt5.shutdown()
            except Exception:
                pass

            if attempt < retries and retry_delay > 0:
                time.sleep(retry_delay)

    return False


def _is_demo_server(server: str) -> bool:
    return "demo" in str(server or "").strip().lower()


def main() -> int:
    args = parse_args()
    config_path = os.path.abspath(args.config)

    if not os.path.isfile(config_path):
        bridge.log(f"Config file not found: {config_path}")
        bridge.log("Copy mt5_config.example.json to mt5_config.json and fill it out.")
        return 1

    try:
        config = bridge.load_config(config_path)
        bridge.log(f"Using config: {config_path}")
        bridge.log(
            f"Config credentials: login={config['login']} server={config['server']} "
            f"symbols={len(config['_parsed_symbols'])}"
        )

        if not args.allow_non_demo and not _is_demo_server(str(config.get("server", ""))):
            bridge.log(
                "[ERROR] Refusing to start live mode on non-demo server. "
                "Use --allow-non-demo to bypass."
            )
            return 1

        if int(args.poll_seconds) > 0:
            config["poll_seconds"] = int(args.poll_seconds)

        config["trade_enabled"] = not bool(args.dry_run)

        if not _authorize_with_retry(
            config=config,
            retries=args.retries,
            retry_delay=args.retry_delay,
            terminal_warmup=args.terminal_warmup,
        ):
            bridge.log("[ERROR] MT5 authorization failed after retries")
            return 1

        bridge.show_account_summary()
        inactive = bridge.ensure_symbols(config["_parsed_symbols"])
        config["_inactive_symbols"] = inactive
        if inactive:
            bridge.log(f"[WARN] Inactive symbols: {', '.join(sorted(inactive))}")

        mode = "LIVE" if bool(config["trade_enabled"]) else "DRY-RUN"
        bridge.log(f"Demo launcher initialized in {mode} mode")
        bridge.log(f"Signals root: {config['signals_root']}")
        bridge.log(f"Polling interval: {config['poll_seconds']}s")

        return bridge.run_loop(config, once=bool(args.once))
    except Exception as e:
        bridge.log(f"[ERROR] {e}")
        return 1
    finally:
        try:
            bridge.mt5.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
