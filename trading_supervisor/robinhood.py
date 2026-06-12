"""
Robinhood account interface built on robin-stocks.

All public methods return typed domain models from models.py so the rest of the
codebase stays decoupled from the raw API response shape.
"""
import logging
from datetime import datetime
from typing import Optional

import robin_stocks.robinhood as rh

from .models import AccountSnapshot, OpenOrder, Position

logger = logging.getLogger(__name__)


class RobinhoodAccount:
    def __init__(self, username: str, password: str, mfa_code: Optional[str] = None):
        """Log in and pin the session. Raises on auth failure."""
        login_kwargs: dict = {"username": username, "password": password, "store_session": True}
        if mfa_code:
            login_kwargs["mfa_code"] = mfa_code
        rh.login(**login_kwargs)
        logger.info("Robinhood session established.")

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def get_snapshot(self) -> AccountSnapshot:
        """Fetch full account state in one pass."""
        equity = self._get_equity()
        cash = self._get_cash()
        positions = self._get_positions()
        open_orders = self._get_open_orders(positions)
        return AccountSnapshot(
            timestamp=datetime.utcnow(),
            equity=equity,
            cash=cash,
            positions=positions,
            open_orders=open_orders,
        )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str, dry_run: bool = False) -> bool:
        """Cancel a single open order. Returns True on success."""
        if dry_run:
            logger.info(f"[DRY RUN] Would cancel order {order_id}")
            return True
        try:
            result = rh.orders.cancel_stock_order(order_id)
            if result is None:
                logger.warning(f"Cancel returned None for order {order_id} (may already be filled/cancelled).")
                return False
            logger.info(f"Cancelled order {order_id}.")
            return True
        except Exception as exc:
            logger.error(f"Failed to cancel order {order_id}: {exc}")
            return False

    def cancel_all_orders(self, dry_run: bool = False) -> list[str]:
        """Cancel every open order. Returns list of cancelled order IDs."""
        if dry_run:
            logger.info("[DRY RUN] Would cancel all open orders.")
            return []
        try:
            rh.orders.cancel_all_stock_orders()
            logger.info("Issued cancel-all for open orders.")
            return []
        except Exception as exc:
            logger.error(f"cancel_all_orders failed: {exc}")
            return []

    def place_market_sell(
        self, symbol: str, quantity: float, dry_run: bool = False
    ) -> Optional[dict]:
        """
        Place a market sell for `quantity` shares of `symbol`.
        Only called by the hard-limit enforcer to trim oversized positions.
        Returns the raw order dict on success, None on failure.
        """
        if dry_run:
            logger.info(f"[DRY RUN] Would sell {quantity} shares of {symbol} at market.")
            return {"id": "dry-run", "symbol": symbol, "quantity": quantity}
        try:
            result = rh.orders.order_sell_market(symbol, quantity)
            logger.info(f"Market sell placed: {quantity} shares of {symbol} → order {result.get('id')}")
            return result
        except Exception as exc:
            logger.error(f"place_market_sell({symbol}, {quantity}) failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Optional[float]:
        """Return last trade price for symbol, or None on error."""
        try:
            data = rh.stocks.get_latest_price(symbol)
            if data and data[0] is not None:
                return float(data[0])
        except Exception as exc:
            logger.warning(f"get_quote({symbol}) failed: {exc}")
        return None

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        """Return {symbol: last_price} for a list of symbols."""
        result = {}
        try:
            prices = rh.stocks.get_latest_price(symbols)
            for sym, price in zip(symbols, prices):
                if price is not None:
                    result[sym] = float(price)
        except Exception as exc:
            logger.warning(f"get_quotes batch failed: {exc}")
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_equity(self) -> float:
        try:
            profile = rh.profiles.load_portfolio_profile()
            return float(profile.get("equity", 0) or 0)
        except Exception as exc:
            logger.error(f"_get_equity failed: {exc}")
            return 0.0

    def _get_cash(self) -> float:
        try:
            profile = rh.profiles.load_account_profile()
            return float(profile.get("portfolio_cash", 0) or 0)
        except Exception as exc:
            logger.error(f"_get_cash failed: {exc}")
            return 0.0

    def _get_positions(self) -> list[Position]:
        try:
            raw = rh.account.build_holdings()
            positions = []
            for symbol, data in raw.items():
                qty = float(data.get("quantity", 0) or 0)
                if qty <= 0:
                    continue
                avg_cost = float(data.get("average_buy_price", 0) or 0)
                price = float(data.get("price", 0) or 0)
                positions.append(Position(symbol=symbol, quantity=qty, average_cost=avg_cost, current_price=price))
            return positions
        except Exception as exc:
            logger.error(f"_get_positions failed: {exc}")
            return []

    def _get_open_orders(self, positions: list[Position]) -> list[OpenOrder]:
        """Fetch open orders and enrich with estimated USD value."""
        try:
            raw_orders = rh.orders.get_all_open_stock_orders()
            position_prices = {p.symbol: p.current_price for p in positions}
            orders = []
            for raw in raw_orders:
                symbol = raw.get("symbol") or ""
                qty = float(raw.get("quantity") or 0)
                limit_price_str = raw.get("price")
                stop_price_str = raw.get("stop_price")
                limit_price = float(limit_price_str) if limit_price_str else None
                stop_price = float(stop_price_str) if stop_price_str else None
                ref_price = limit_price or stop_price or position_prices.get(symbol, 0)
                orders.append(
                    OpenOrder(
                        order_id=raw.get("id", ""),
                        symbol=symbol,
                        side=raw.get("side", ""),
                        order_type=raw.get("type", ""),
                        quantity=qty,
                        limit_price=limit_price,
                        stop_price=stop_price,
                        estimated_value=qty * ref_price,
                    )
                )
            return orders
        except Exception as exc:
            logger.error(f"_get_open_orders failed: {exc}")
            return []
