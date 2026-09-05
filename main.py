"""CLI: tradingbot run | status | once"""

from __future__ import annotations

import argparse
import logging
import sys

from tradingbot.bot import TradingBot
from tradingbot.config import load_settings


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)sZ %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Quiet noisy libs
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args.env)
    bot = TradingBot(settings)
    bot.run()
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    settings = load_settings(args.env)
    bot = TradingBot(settings)
    bot.tick()
    print(
        f"once complete | dry_run={settings.dry_run} | "
        f"daily_pnl={bot.risk.realized_pnl_today:.4f} | "
        f"halt={bot.risk.daily_loss_halted} | position={bot.position}"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings(args.env)
    bot = TradingBot(settings)
    print("=== Tradingbot status ===")
    print(f"symbol:              {settings.symbol}")
    print(f"DRY_RUN:             {settings.dry_run}")
    print(f"MAX_POSITION_USD:    {settings.max_position_usd}")
    print(f"MAX_DAILY_LOSS_USD:  {settings.max_daily_loss_usd}")
    print(f"STOP_LOSS_PCT:       {settings.stop_loss_pct}")
    print(f"realized PnL (UTC):  {bot.risk.realized_pnl_today:.4f}")
    print(f"daily loss halt:     {bot.risk.daily_loss_halted}")
    print(f"open position:       {bot.position}")
    print(
        "Note: soft $ target is metrics/logging only — not a profit guarantee."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tradingbot",
        description="Defensive Binance SPOT bot with hard risk caps (no profit guarantee).",
    )
    p.add_argument("--env", default=None, help="Path to .env file")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the polling loop")
    run_p.set_defaults(func=cmd_run)

    once_p = sub.add_parser("once", help="Single tick then exit")
    once_p.set_defaults(func=cmd_once)

    st_p = sub.add_parser("status", help="Show risk caps and position")
    st_p.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        code = args.func(args)
    except ValueError as exc:
        logging.error("%s", exc)
        sys.exit(2)
    sys.exit(code)


if __name__ == "__main__":
    main()
