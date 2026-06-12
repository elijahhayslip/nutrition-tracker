"""Configuration and hard limits — all values readable from environment variables."""
import os
from dataclasses import dataclass, field


@dataclass
class HardLimits:
    """
    Non-negotiable constraints enforced in Python before any order action.
    The AI agent cannot override these — they are checked in pure Python code
    that runs independently of the LLM reasoning loop.
    """
    # Maximum allowable loss from today's session-open equity (%).
    max_daily_loss_pct: float = 2.0

    # Maximum drawdown from the all-time peak equity recorded this session (%).
    max_drawdown_pct: float = 5.0

    # Maximum single-position size as a fraction of total equity (%).
    max_position_size_pct: float = 20.0

    # Maximum number of simultaneously open equity positions.
    max_open_positions: int = 10

    # Minimum cash balance expressed as % of total equity — never go below this.
    min_cash_reserve_pct: float = 10.0

    # Maximum USD value of any single order the agent is allowed to place.
    max_single_order_usd: float = 5_000.0

    @classmethod
    def from_env(cls) -> "HardLimits":
        return cls(
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "2.0")),
            max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "5.0")),
            max_position_size_pct=float(os.getenv("MAX_POSITION_SIZE_PCT", "20.0")),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "10")),
            min_cash_reserve_pct=float(os.getenv("MIN_CASH_RESERVE_PCT", "10.0")),
            max_single_order_usd=float(os.getenv("MAX_SINGLE_ORDER_USD", "5000.0")),
        )

    def describe(self) -> str:
        return (
            f"Hard limits in effect:\n"
            f"  max_daily_loss      = {self.max_daily_loss_pct}%\n"
            f"  max_drawdown        = {self.max_drawdown_pct}%\n"
            f"  max_position_size   = {self.max_position_size_pct}%\n"
            f"  max_open_positions  = {self.max_open_positions}\n"
            f"  min_cash_reserve    = {self.min_cash_reserve_pct}%\n"
            f"  max_single_order    = ${self.max_single_order_usd:,.2f}"
        )


@dataclass
class Config:
    # How often the supervisor polls the account (seconds).
    poll_interval_seconds: int = 300

    # Use Claude for AI-assisted analysis in addition to rule-based checks.
    enable_ai_analysis: bool = True

    # Automatically trim positions that exceed max_position_size_pct.
    enable_auto_trim: bool = True

    # Write an append-only JSONL audit log to this file.
    audit_log_file: str = "supervisor_audit.jsonl"

    # Persist session state (peak equity, daily open) between restarts.
    state_file: str = "supervisor_state.json"

    # When True, log all actions but execute no real orders.
    dry_run: bool = False

    limits: HardLimits = field(default_factory=HardLimits)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "300")),
            enable_ai_analysis=os.getenv("ENABLE_AI_ANALYSIS", "true").lower() == "true",
            enable_auto_trim=os.getenv("ENABLE_AUTO_TRIM", "true").lower() == "true",
            audit_log_file=os.getenv("AUDIT_LOG_FILE", "supervisor_audit.jsonl"),
            state_file=os.getenv("STATE_FILE", "supervisor_state.json"),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            limits=HardLimits.from_env(),
        )
