"""
Entry point for the Trading Account Supervisor.

Usage:
  python -m trading_supervisor.main [options]

Environment variables (all optional — defaults shown):
  ROBINHOOD_USERNAME       Robinhood account email
  ROBINHOOD_PASSWORD       Robinhood account password
  ROBINHOOD_MFA_CODE       TOTP code if MFA is enabled
  ANTHROPIC_API_KEY        Anthropic API key for AI analysis

  MAX_DAILY_LOSS_PCT       2.0   Stop trading if down >N% on the day
  MAX_DRAWDOWN_PCT         5.0   Stop trading if down >N% from session peak
  MAX_POSITION_SIZE_PCT   20.0   Trim any position > N% of equity
  MAX_OPEN_POSITIONS       10    Cancel new orders if positions exceed N
  MIN_CASH_RESERVE_PCT    10.0   Cancel buy orders if cash falls below N%
  MAX_SINGLE_ORDER_USD   5000.0  Block any order > $N

  POLL_INTERVAL_SECONDS   300    Seconds between supervision cycles
  ENABLE_AI_ANALYSIS      true   Set to false to run rule-only mode
  ENABLE_AUTO_TRIM        true   Set to false to alert only (no auto-sell)
  DRY_RUN                 false  Set to true to log actions without executing
  AUDIT_LOG_FILE          supervisor_audit.jsonl
  STATE_FILE              supervisor_state.json
"""
import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .agent import TradingSupervisor
from .config import Config
from .robinhood import RobinhoodAccount


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt, handlers=handlers)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Autonomous trading account supervisor with hard limits.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--dry-run", action="store_true", help="Log actions without placing real orders.")
    p.add_argument("--no-ai", action="store_true", help="Disable AI analysis; run rule-based mode only.")
    p.add_argument("--interval", type=int, default=None, help="Poll interval in seconds (default: 300).")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--env-file", default=".env", help="Path to .env file (default: .env).")
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single supervision cycle and exit (useful for cron/testing).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Load environment variables
    env_path = Path(args.env_file)
    if env_path.exists():
        load_dotenv(env_path)

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Build config from env (CLI flags override)
    config = Config.from_env()
    if args.dry_run:
        config.dry_run = True
    if args.no_ai:
        config.enable_ai_analysis = False
    if args.interval is not None:
        config.poll_interval_seconds = args.interval

    if config.dry_run:
        logger.warning("DRY RUN mode — no real orders will be placed.")

    # Credentials
    username = os.getenv("ROBINHOOD_USERNAME")
    password = os.getenv("ROBINHOOD_PASSWORD")
    mfa_code = os.getenv("ROBINHOOD_MFA_CODE")

    if not username or not password:
        logger.error(
            "ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD must be set "
            "(in environment or .env file)."
        )
        sys.exit(1)

    # Connect
    logger.info("Connecting to Robinhood...")
    account = RobinhoodAccount(username=username, password=password, mfa_code=mfa_code)

    supervisor = TradingSupervisor(account=account, config=config)

    if args.once:
        supervisor._init_session()
        supervisor._supervision_cycle()
    else:
        supervisor.run()


if __name__ == "__main__":
    main()
