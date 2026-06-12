"""
TradingSupervisor — autonomous account watchdog.

Architecture
------------
Each supervision cycle runs in two phases:

Phase 1 — Hard-limit enforcement (pure Python, no LLM):
  * Fetch account snapshot from Robinhood.
  * Run LimitChecker.check_all() against the snapshot.
  * For each violation, execute the matching enforcement action immediately.
  * Cancel all open buy orders if daily-loss or drawdown limit is breached.
  * Trim oversized positions by selling the excess at market.

Phase 2 — AI analysis (Claude tool-use loop, runs only if limits are clean):
  * Feed the snapshot and any cleared violations to Claude.
  * Claude can call read-only and protective tools: get_snapshot, get_quote,
    cancel_order, trim_position, log_alert.
  * Claude CANNOT place buy orders — that tool is absent from the schema.
  * Before any sell executes, the order is re-validated against hard limits.

Every action is written to an append-only JSONL audit log.
"""
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import anthropic

from .config import Config
from .limits import LimitChecker
from .models import AccountSnapshot, LimitViolation, ViolationType
from .robinhood import RobinhoodAccount

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SUPERVISOR_SYSTEM_PROMPT = """\
You are an autonomous trading account supervisor.  Your only mandate is to
PROTECT the account from losses — you do not look for new opportunities or
suggest buy trades.

You have access to these tools:
  • get_snapshot        — read current account state (positions, cash, orders)
  • get_quote           — look up the current price of any symbol
  • cancel_order        — cancel a single open order by order_id
  • cancel_all_orders   — cancel every open order (use for emergency situations)
  • trim_position       — sell a specified number of shares to reduce a position
  • log_alert           — record a note in the audit log

Rules you must follow:
1. Never suggest buying or increasing any position.
2. Only recommend protective actions: cancelling orders, reducing positions.
3. Every action you take is logged; explain your reasoning briefly.
4. If you detect unusual concentration, overlapping stop-losses, or pending
   orders that would breach a hard limit when filled, act proactively.
5. Finish each cycle with a concise status summary using log_alert.
"""


# ---------------------------------------------------------------------------
# Claude tool definitions (buy-side tools are deliberately absent)
# ---------------------------------------------------------------------------

AGENT_TOOLS = [
    {
        "name": "get_snapshot",
        "description": (
            "Return the current account snapshot: equity, cash, all open positions "
            "with market value and P&L, and all open orders."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_quote",
        "description": "Return the current last-trade price for a stock symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker symbol, e.g. AAPL"}
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "cancel_order",
        "description": "Cancel a single open order by its order_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "UUID of the open order"},
                "reason": {"type": "string", "description": "Brief reason for cancellation"},
            },
            "required": ["order_id", "reason"],
        },
    },
    {
        "name": "cancel_all_orders",
        "description": (
            "Cancel every open order on the account.  Use only in an emergency "
            "(e.g. daily-loss limit is about to be hit)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Brief reason"}
            },
            "required": ["reason"],
        },
    },
    {
        "name": "trim_position",
        "description": (
            "Sell shares of a position to reduce its size.  Only use this to "
            "enforce risk limits — never to time the market."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker symbol"},
                "shares": {
                    "type": "number",
                    "description": "Number of shares to sell (fractional allowed)",
                },
                "reason": {"type": "string", "description": "Why this trim is needed"},
            },
            "required": ["symbol", "shares", "reason"],
        },
    },
    {
        "name": "log_alert",
        "description": "Write a timestamped alert or status note to the audit log.",
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["INFO", "WARNING", "CRITICAL"],
                    "description": "Severity level",
                },
                "message": {"type": "string", "description": "Alert text"},
            },
            "required": ["level", "message"],
        },
    },
]


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class TradingSupervisor:
    def __init__(self, account: RobinhoodAccount, config: Config):
        self.account = account
        self.config = config
        self.client = anthropic.Anthropic()
        self._checker: Optional[LimitChecker] = None
        self._audit_log = Path(config.audit_log_file)
        self._state_file = Path(config.state_file)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Start the infinite supervision loop.
        Each cycle: fetch → enforce limits → AI analysis → sleep.
        """
        logger.info("Starting Trading Supervisor.")
        logger.info(self.config.limits.describe())
        self._init_session()

        while True:
            try:
                self._supervision_cycle()
            except KeyboardInterrupt:
                logger.info("Supervisor stopped by user.")
                break
            except Exception as exc:
                logger.exception(f"Unhandled error in supervision cycle: {exc}")
                self._audit("CYCLE_ERROR", {"error": str(exc)})

            logger.info(f"Sleeping {self.config.poll_interval_seconds}s until next cycle...")
            time.sleep(self.config.poll_interval_seconds)

    # ------------------------------------------------------------------
    # Session init
    # ------------------------------------------------------------------

    def _init_session(self) -> None:
        """Load or create session state (peak equity, daily open equity)."""
        state = self._load_state()
        snapshot = self.account.get_snapshot()
        equity = snapshot.equity

        today = date.today().isoformat()
        session_open = state.get("session_open_equity", equity)
        # Reset daily baseline if it's a new calendar day
        if state.get("last_date") != today:
            session_open = equity
            logger.info(f"New trading day — session open equity: ${equity:,.2f}")

        peak = max(state.get("peak_equity", equity), equity)
        self._checker = LimitChecker(
            limits=self.config.limits,
            session_open_equity=session_open,
            peak_equity=peak,
        )
        self._save_state(today, session_open, peak)
        logger.info(
            f"Session open equity: ${session_open:,.2f}  |  Peak equity: ${peak:,.2f}"
        )

    # ------------------------------------------------------------------
    # Core supervision cycle
    # ------------------------------------------------------------------

    def _supervision_cycle(self) -> None:
        logger.info("── Supervision cycle starting ──────────────────────────")

        # --- Phase 1: hard-limit enforcement (Python only) ---
        snapshot = self.account.get_snapshot()
        self._checker.update_peak(snapshot.equity)
        self._save_state(
            date.today().isoformat(),
            self._checker.session_open_equity,
            self._checker.peak_equity,
        )

        violations = self._checker.check_all(snapshot)
        self._audit(
            "SNAPSHOT",
            {
                **snapshot.to_dict(),
                "session_open_equity": self._checker.session_open_equity,
                "peak_equity": self._checker.peak_equity,
                "violations": [v.to_dict() for v in violations],
            },
        )

        if violations:
            logger.warning(f"{len(violations)} hard-limit violation(s) detected.")
            for v in violations:
                logger.critical(f"VIOLATION: {v.message}")
            self._enforce_violations(violations, snapshot)
        else:
            logger.info("All hard limits satisfied.")

        # --- Phase 2: AI analysis ---
        if self.config.enable_ai_analysis:
            self._ai_analysis_cycle(snapshot, violations)

        logger.info("── Supervision cycle complete ───────────────────────────")

    # ------------------------------------------------------------------
    # Hard-limit enforcement (Python, no LLM)
    # ------------------------------------------------------------------

    def _enforce_violations(
        self, violations: list[LimitViolation], snapshot: AccountSnapshot
    ) -> None:
        for violation in violations:
            self._audit("VIOLATION", violation.to_dict())

            if violation.type in (
                ViolationType.DAILY_LOSS_EXCEEDED,
                ViolationType.DRAWDOWN_EXCEEDED,
            ):
                logger.critical(
                    f"[HARD LIMIT] {violation.type.value} — cancelling all open orders."
                )
                self.account.cancel_all_orders(dry_run=self.config.dry_run)
                self._audit(
                    "ACTION",
                    {
                        "action": "CANCEL_ALL_ORDERS",
                        "reason": violation.type.value,
                        "violation": violation.message,
                    },
                )

            elif violation.type == ViolationType.POSITION_SIZE_EXCEEDED:
                if self.config.enable_auto_trim and violation.excess_shares:
                    sym = violation.symbol
                    excess = round(violation.excess_shares, 4)
                    logger.warning(
                        f"[HARD LIMIT] Trimming {sym}: selling {excess} excess shares."
                    )
                    # Re-validate the trim itself doesn't exceed the single-order limit
                    pos = next((p for p in snapshot.positions if p.symbol == sym), None)
                    if pos:
                        sell_value = excess * pos.current_price
                        size_check = self._checker.check_order_size(sell_value)
                        if size_check is None:
                            result = self.account.place_market_sell(
                                sym, excess, dry_run=self.config.dry_run
                            )
                            self._audit(
                                "ACTION",
                                {
                                    "action": "TRIM_POSITION",
                                    "symbol": sym,
                                    "shares": excess,
                                    "reason": violation.type.value,
                                    "order": result,
                                },
                            )
                        else:
                            logger.warning(
                                f"Trim sell value ${sell_value:,.2f} would exceed "
                                f"single-order limit — splitting not yet supported, skipping auto-trim."
                            )

            elif violation.type == ViolationType.CASH_RESERVE_VIOLATED:
                # Cancel buy-side orders to free up buying power
                buy_orders = [o for o in snapshot.open_orders if o.side == "buy"]
                for order in buy_orders:
                    logger.warning(
                        f"[HARD LIMIT] Cancelling buy order {order.order_id} "
                        f"({order.symbol}) — cash reserve violated."
                    )
                    self.account.cancel_order(order.order_id, dry_run=self.config.dry_run)
                    self._audit(
                        "ACTION",
                        {
                            "action": "CANCEL_BUY_ORDER",
                            "order_id": order.order_id,
                            "symbol": order.symbol,
                            "reason": violation.type.value,
                        },
                    )

    # ------------------------------------------------------------------
    # AI analysis cycle (Claude tool-use loop)
    # ------------------------------------------------------------------

    def _ai_analysis_cycle(
        self, snapshot: AccountSnapshot, violations: list[LimitViolation]
    ) -> None:
        """Run Claude over the current snapshot.  Claude may call protective tools."""
        context = self._build_ai_context(snapshot, violations)
        messages: list[dict] = [{"role": "user", "content": context}]
        max_rounds = 10

        for round_num in range(max_rounds):
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=SUPERVISOR_SYSTEM_PROMPT,
                tools=AGENT_TOOLS,
                messages=messages,
            )

            # Append assistant turn to conversation
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                # Extract and log any final text
                for block in response.content:
                    if hasattr(block, "text") and block.text:
                        logger.info(f"AI summary: {block.text[:400]}")
                        self._audit("AI_SUMMARY", {"text": block.text})
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = self._dispatch_tool(block.name, block.input, snapshot)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            }
                        )
                        # Refresh snapshot after any mutating tool call
                        if block.name in ("cancel_order", "cancel_all_orders", "trim_position"):
                            snapshot = self.account.get_snapshot()

                messages.append({"role": "user", "content": tool_results})
            else:
                logger.warning(f"Unexpected stop_reason: {response.stop_reason}")
                break

    def _dispatch_tool(
        self, name: str, inputs: dict[str, Any], snapshot: AccountSnapshot
    ) -> Any:
        """Route a Claude tool call to the appropriate implementation."""
        self._audit("TOOL_CALL", {"tool": name, "inputs": inputs})

        if name == "get_snapshot":
            fresh = self.account.get_snapshot()
            return fresh.to_dict()

        elif name == "get_quote":
            symbol = inputs["symbol"]
            price = self.account.get_quote(symbol)
            return {"symbol": symbol, "price": price}

        elif name == "cancel_order":
            order_id = inputs["order_id"]
            reason = inputs.get("reason", "AI supervisor request")
            ok = self.account.cancel_order(order_id, dry_run=self.config.dry_run)
            self._audit(
                "ACTION",
                {"action": "CANCEL_ORDER", "order_id": order_id, "reason": reason, "success": ok},
            )
            return {"success": ok, "order_id": order_id}

        elif name == "cancel_all_orders":
            reason = inputs.get("reason", "AI supervisor request")
            self.account.cancel_all_orders(dry_run=self.config.dry_run)
            self._audit("ACTION", {"action": "CANCEL_ALL_ORDERS", "reason": reason})
            return {"success": True}

        elif name == "trim_position":
            symbol = inputs["symbol"]
            shares = float(inputs["shares"])
            reason = inputs.get("reason", "AI supervisor trim")

            # Hard-limit check before executing
            pos = next((p for p in snapshot.positions if p.symbol == symbol), None)
            if pos is None:
                return {"success": False, "error": f"No open position for {symbol}"}
            if shares > pos.quantity:
                return {
                    "success": False,
                    "error": f"Cannot sell {shares} shares — only {pos.quantity} held.",
                }
            sell_value = shares * pos.current_price
            size_violation = self._checker.check_order_size(sell_value)
            if size_violation:
                return {"success": False, "error": size_violation.message}

            result = self.account.place_market_sell(
                symbol, shares, dry_run=self.config.dry_run
            )
            self._audit(
                "ACTION",
                {
                    "action": "TRIM_POSITION",
                    "symbol": symbol,
                    "shares": shares,
                    "reason": reason,
                    "order": result,
                },
            )
            return {"success": result is not None, "order": result}

        elif name == "log_alert":
            level = inputs.get("level", "INFO")
            message = inputs.get("message", "")
            log_fn = {"INFO": logger.info, "WARNING": logger.warning, "CRITICAL": logger.critical}
            log_fn.get(level, logger.info)(f"[AI ALERT] {message}")
            self._audit("AI_ALERT", {"level": level, "message": message})
            return {"logged": True}

        else:
            logger.error(f"Unknown tool: {name}")
            return {"error": f"Unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_ai_context(
        self, snapshot: AccountSnapshot, violations: list[LimitViolation]
    ) -> str:
        limits = self.config.limits
        violation_text = (
            "\n".join(f"  • {v.message}" for v in violations)
            if violations
            else "  None — all limits satisfied."
        )
        return (
            f"Supervision cycle at {snapshot.timestamp.isoformat()} UTC\n\n"
            f"HARD LIMITS:\n{limits.describe()}\n\n"
            f"CURRENT VIOLATIONS:\n{violation_text}\n\n"
            f"ACCOUNT SNAPSHOT:\n{json.dumps(snapshot.to_dict(), indent=2)}\n\n"
            f"SESSION STATS:\n"
            f"  Session open equity : ${self._checker.session_open_equity:,.2f}\n"
            f"  Peak equity         : ${self._checker.peak_equity:,.2f}\n\n"
            "Review the account for risk and take any needed protective actions."
        )

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def _audit(self, event_type: str, payload: dict) -> None:
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "event": event_type,
            **payload,
        }
        line = json.dumps(entry)
        logger.debug(f"AUDIT: {line}")
        with self._audit_log.open("a") as fh:
            fh.write(line + "\n")

    # ------------------------------------------------------------------
    # Persistent state
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text())
            except Exception:
                pass
        return {}

    def _save_state(self, today: str, session_open: float, peak: float) -> None:
        state = {
            "last_date": today,
            "session_open_equity": session_open,
            "peak_equity": peak,
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._state_file.write_text(json.dumps(state, indent=2))
