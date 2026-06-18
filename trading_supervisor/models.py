"""Shared domain models."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ViolationType(str, Enum):
    DAILY_LOSS_EXCEEDED = "DAILY_LOSS_EXCEEDED"
    DRAWDOWN_EXCEEDED = "DRAWDOWN_EXCEEDED"
    POSITION_SIZE_EXCEEDED = "POSITION_SIZE_EXCEEDED"
    POSITION_COUNT_EXCEEDED = "POSITION_COUNT_EXCEEDED"
    CASH_RESERVE_VIOLATED = "CASH_RESERVE_VIOLATED"
    ORDER_SIZE_EXCEEDED = "ORDER_SIZE_EXCEEDED"


@dataclass
class LimitViolation:
    type: ViolationType
    message: str
    symbol: Optional[str] = None
    current_value: Optional[float] = None
    limit_value: Optional[float] = None
    excess_shares: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "message": self.message,
            "symbol": self.symbol,
            "current_value": self.current_value,
            "limit_value": self.limit_value,
        }


@dataclass
class Position:
    symbol: str
    quantity: float
    average_cost: float
    current_price: float

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.average_cost) * self.quantity

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.average_cost == 0:
            return 0.0
        return ((self.current_price - self.average_cost) / self.average_cost) * 100

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "average_cost": self.average_cost,
            "current_price": self.current_price,
            "market_value": round(self.market_value, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "unrealized_pnl_pct": round(self.unrealized_pnl_pct, 2),
        }


@dataclass
class OpenOrder:
    order_id: str
    symbol: str
    side: str          # 'buy' | 'sell'
    order_type: str    # 'market' | 'limit' | 'stop_market' | 'stop_limit'
    quantity: float
    limit_price: Optional[float]
    stop_price: Optional[float]
    estimated_value: float

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "estimated_value": round(self.estimated_value, 2),
        }


@dataclass
class AccountSnapshot:
    timestamp: datetime
    equity: float       # total portfolio value
    cash: float         # buying power / uninvested cash
    positions: list[Position]
    open_orders: list[OpenOrder]

    @property
    def invested_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def cash_pct(self) -> float:
        if self.equity == 0:
            return 0.0
        return (self.cash / self.equity) * 100

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "cash_pct": round(self.cash_pct, 2),
            "invested_value": round(self.invested_value, 2),
            "position_count": len(self.positions),
            "open_order_count": len(self.open_orders),
            "positions": [p.to_dict() for p in self.positions],
            "open_orders": [o.to_dict() for o in self.open_orders],
        }
