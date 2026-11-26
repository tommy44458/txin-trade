from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from .indicator_preferences import InitialIndicatorName, normalize_initial_indicators
from .market_catalog import validate_market_id
from .prompts.contracts import LEFT_SIDE_STRATEGIES, TradingStrategy

Direction = Literal["long", "short"]
MarketType = Literal["linear_perpetual"]
MarketId = Annotated[str, Field(max_length=80), AfterValidator(validate_market_id)]
Timeframe = Literal["1h", "4h", "12h", "1d"]
OutputLocale = Literal["zh-TW", "en-US"]


class AnalysisRequest(BaseModel):
    kind: Literal["market", "positions"] = "market"
    market_id: MarketId
    timeframe: Timeframe = "1h"
    directional_bias: Literal["bullish", "bearish"] | None = None
    risk_tolerance: Literal["low", "medium", "high"] | None = None
    trading_style: Literal["left", "right"] | None = None
    # The chosen strategy loads its skill into the prompt; none keeps the general analysis.
    trading_strategy: TradingStrategy | None = None
    leverage: int = Field(default=5, ge=1, le=125)
    position_ids: list[str] = Field(default_factory=list)
    account_equity_usdt: str | None = None
    output_locale: OutputLocale = "zh-TW"
    # None inherits settings only when creating a new job; [] explicitly opts
    # out. Stored jobs always freeze a list and never reread later preferences.
    initial_indicators: list[InitialIndicatorName] | None = Field(default=None, max_length=100)

    @field_validator("initial_indicators")
    @classmethod
    def validate_initial_indicators(cls, value):
        return normalize_initial_indicators(value) if value is not None else None

    @field_validator("account_equity_usdt")
    @classmethod
    def validate_equity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            equity = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Account equity must be a positive finite USDT amount") from exc
        if not equity.is_finite() or not 0 < equity <= Decimal(1000000000000):
            raise ValueError("Account equity must be a positive finite USDT amount")
        return str(equity)


    @model_validator(mode="after")
    def strategy_sets_entry_style(self):
        # A left-side strategy decides the entry style, so a stale style cannot contradict it.
        if self.trading_strategy in LEFT_SIDE_STRATEGIES:
            self.trading_style = "left"
        return self

    @model_validator(mode="after")
    def validate_positions(self):
        if self.kind == "market" and (self.position_ids or self.account_equity_usdt is not None):
            raise ValueError("Market analysis cannot include positions or account equity")
        if self.kind == "positions" and not self.position_ids:
            raise ValueError("Select at least one position")
        if len(set(self.position_ids)) != len(self.position_ids):
            raise ValueError("Duplicate position IDs")
        return self


class PositionInput(BaseModel):
    market_id: MarketId
    side: Direction = "long"
    leverage: int = Field(default=5, ge=1, le=125)
    margin_mode: Literal["isolated", "cross"] = "isolated"
    entry_price: str
    quantity: str
    stop_loss: str | None = None
    take_profit: str | None = None
    entry_time: datetime | None = None
    exchange_liquidation_price: str | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("notes")
    @classmethod
    def clean_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_entry_time(self):
        if (self.entry_time is not None and
                (self.entry_time.tzinfo is None or
                 self.entry_time > datetime.now(UTC) + timedelta(minutes=5))):
            raise ValueError("Entry time must include a timezone and cannot be in the future")
        return self


class PositionUpdate(PositionInput):
    expected_version: int = Field(ge=1)
