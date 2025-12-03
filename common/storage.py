import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple

from sqlalchemy import desc
from sqlmodel import Field, SQLModel, create_engine, Session, select

# Default path used for SQLite fallback (when no MySQL env vars are provided)
DB_PATH = Path("../bot/data") / "trading.db"


def get_db_url() -> str:
    """
    Returns the database connection URL for SQLModel/SQLAlchemy.

    - If environment variables MYSQL_HOST (and optionally MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB, MYSQL_PORT) are present,
      MySQL is used (for example, an AWS RDS instance).
    - Otherwise SQLite is used locally at data/trading.db.
    """
    mysql_host = os.getenv("MYSQL_HOST")
    if mysql_host:
        user = os.getenv("MYSQL_USER", "trading")
        password = os.getenv("MYSQL_PASSWORD", "")
        db_name = os.getenv("MYSQL_DB", "trading")
        port = os.getenv("MYSQL_PORT", "3306")
        return f"mysql+pymysql://{user}:{password}@{mysql_host}:{port}/{db_name}"
    # Fallback: SQLite local
    return f"sqlite:///{DB_PATH}"


class Trade(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: str = Field(index=True)
    symbol: str
    side: str  # "buy" / "sell"
    price: float
    amount: float
    mode: str  # "paper" / "live"
    pnl: Optional[float] = None
    bot_type: str = Field(default=None, index=True)


class Equity(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: str = Field(index=True)
    equity: float


class Decision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: str = Field(index=True)
    symbol: str
    action: str  # "buy" / "sell" / "hold"
    confidence: float
    ma_ratio: float
    rsi_14: float
    vol_20: float
    mode: str  # "paper" / "live"
    equity_before: Optional[float] = None
    price: Optional[float] = None
    candle_ts: Optional[str] = None
    bot_type: str = Field(default=None, index=True)

    outcome_pnl_usdt: Optional[float] = None
    outcome_pnl_pct: Optional[float] = None
    outcome_equity_delta: Optional[float] = None
    outcome_label: Optional[str] = Field(default=None, index=True)


class BotConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bot_type: str = Field(index=True)  # e.g. "foundational", "scalping"
    key: str = Field(index=True)  # e.g. "min_confidence", "tp_pct"
    value: str  # stored as string, cast on read


class OpenPosition(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: str = Field(index=True)
    symbol: str
    side: str  # "buy" for our current scalper
    amount: float
    entry_price: float
    entry_fee_usdt: float = 0.0
    mode: str = Field(index=True)  # "paper" / "live"
    bot_type: str = Field(default=None, index=True)


class Storage:
    """
    Simple storage wrapper using SQLModel (ORM over SQLite or MySQL).

    - Uses SQLite by default unless MYSQL_* environment variables are supplied.
    - Supports a logical `bot_type` dimension, so multiple bots can write to
      the same tables without mixing their records.
    - Public methods:
        - log_trade(...)
        - log_equity(...)
        - log_decision(...)
        - get_last_buy(...)
        - get_equity_curve()
        - close()
    """

    def __init__(self, bot_type: str, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path

        self.db_url = get_db_url()
        self.bot_type = bot_type

        print("Db engine detected:", self.db_url)
        print("Bot type:", self.bot_type)

        connect_args = {"check_same_thread": False} if self.db_url.startswith("sqlite") else {}
        self.engine = create_engine(
            self.db_url,
            echo=False,
            connect_args=connect_args,
        )
        # Create tables if they do not exist
        SQLModel.metadata.create_all(self.engine)

    def _get_session(self) -> Session:
        return Session(self.engine)

    # -------------------------------------------------------------------------
    # Trades
    # -------------------------------------------------------------------------
    def log_trade(
            self,
            symbol: str,
            side: str,
            price: float,
            amount: float,
            mode: str = "paper",
            pnl: Optional[float] = None,
            bot_type: Optional[str] = None,
    ) -> None:
        """
        Persist a trade in the DB.

        - mode: "paper" or "live"
        - bot_type: label of the bot ("foundational", "scalping", etc.).
          If not provided, defaults to Storage.bot_type.
        """
        ts = datetime.utcnow().isoformat()
        effective_bot_type = bot_type or self.bot_type

        trade = Trade(
            timestamp=ts,
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
            mode=mode,
            pnl=pnl,
            bot_type=effective_bot_type,
        )
        with self._get_session() as session:
            session.add(trade)
            session.commit()

    def get_last_trade(
            self,
            symbol: str,
            side: str,
            mode: str = "live",
    ) -> Optional[Trade]:
        """
        Returns the most recent trade for the given symbol, side, trading mode,
        and current Storage.bot_type, or None if there is no record.
        """
        with self._get_session() as session:
            statement = (
                select(Trade)
                .where(
                    Trade.symbol == symbol,
                    Trade.side == side,
                    Trade.mode == mode,
                    Trade.bot_type == self.bot_type,
                )
                .order_by(desc(Trade.timestamp))
                .limit(1)
            )
            result = session.exec(statement)
            return result.first()

    def get_last_buy(self, symbol: str, mode: str = "live") -> Optional[Trade]:
        """
        Convenience wrapper: returns the most recent BUY trade.
        """
        return self.get_last_trade(symbol=symbol, side="buy", mode=mode)

    def get_last_sell(self, symbol: str, mode: str = "live") -> Optional[Trade]:
        """
        Convenience wrapper: returns the most recent SELL trade.
        """
        return self.get_last_trade(symbol=symbol, side="sell", mode=mode)

    # -------------------------------------------------------------------------
    # Equity
    # -------------------------------------------------------------------------
    def log_equity(self, equity: float) -> None:
        ts = datetime.utcnow().isoformat()
        equity_row = Equity(timestamp=ts, equity=equity)
        with self._get_session() as session:
            session.add(equity_row)
            session.commit()

    def get_equity_curve(self) -> List[Tuple[str, float]]:
        with self._get_session() as session:
            stmt = select(Equity).order_by(Equity.timestamp.asc())
            rows = session.exec(stmt).all()
            return [(row.timestamp, row.equity) for row in rows]

    # -------------------------------------------------------------------------
    # Decisions
    # -------------------------------------------------------------------------
    def log_decision(
            self,
            symbol: str,
            action: str,
            confidence: float,
            ma_ratio: float,
            rsi_14: float,
            vol_20: float,
            mode: str,
            equity_before: Optional[float] = None,
            price: Optional[float] = None,
            candle_ts: Optional[str] = None,
            bot_type: Optional[str] = None,
    ) -> None:
        """
        Persist a model decision and its feature context.

        We log:
          - symbol, action, confidence
          - feature values (ma_ratio, rsi_14, vol_20)
          - mode ("paper" / "live")
          - equity_before: total equity right before applying the decision
          - price: market price at the moment of the decision
          - candle_ts: candle timestamp (as string) used for this decision
          - bot_type: logical bot label ("foundational", "scalping", etc.)
        """
        ts = datetime.utcnow().isoformat()
        effective_bot_type = bot_type or self.bot_type

        decision = Decision(
            timestamp=ts,
            symbol=symbol,
            action=action,
            confidence=confidence,
            ma_ratio=ma_ratio,
            rsi_14=rsi_14,
            vol_20=vol_20,
            mode=mode,
            equity_before=equity_before,
            price=price,
            candle_ts=candle_ts,
            bot_type=effective_bot_type,
        )
        with self._get_session() as session:
            session.add(decision)
            session.commit()

    def add_open_position(
            self,
            *,
            symbol: str,
            side: str,
            amount: float,
            entry_price: float,
            entry_fee_usdt: float = 0.0,
            mode: str = "paper",
            bot_type: Optional[str] = None,
    ) -> int:
        ts = datetime.utcnow().isoformat()
        effective_bot_type = bot_type or self.bot_type

        row = OpenPosition(
            timestamp=ts,
            symbol=symbol,
            side=side,
            amount=amount,
            entry_price=entry_price,
            entry_fee_usdt=entry_fee_usdt,
            mode=mode,
            bot_type=effective_bot_type,
        )
        with self._get_session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id

    def remove_open_position(self, position_id: int) -> None:
        with self._get_session() as session:
            row = session.get(OpenPosition, position_id)
            if row is not None:
                session.delete(row)
                session.commit()

    def get_open_positions(self, mode: Optional[str] = None) -> List[OpenPosition]:
        with self._get_session() as session:
            stmt = select(OpenPosition).where(OpenPosition.bot_type == self.bot_type)
            if mode is not None:
                stmt = stmt.where(OpenPosition.mode == mode)
            stmt = stmt.order_by(OpenPosition.timestamp.asc())
            rows = session.exec(stmt).all()
            return list(rows)

    def get_open_position_for_symbol(
        self,
        symbol: str,
        mode: Optional[str] = None,
    ) -> Optional[OpenPosition]:
        """
        Returns the most recent OpenPosition row for the given symbol and this
        Storage.bot_type, optionally filtered by mode.
        """
        with self._get_session() as session:
            stmt = select(OpenPosition).where(OpenPosition.bot_type == self.bot_type)
            stmt = stmt.where(OpenPosition.symbol == symbol)
            if mode is not None:
                stmt = stmt.where(OpenPosition.mode == mode)
            stmt = stmt.order_by(OpenPosition.timestamp.desc()).limit(1)
            row = session.exec(stmt).first()
            return row

    def update_open_position(
        self,
        position_id: int,
        *,
        amount: Optional[float] = None,
        entry_price: Optional[float] = None,
        entry_fee_usdt: Optional[float] = None,
    ) -> None:
        """
        Updates fields of an existing OpenPosition row by id. Fields left as
        None will not be modified.
        """
        with self._get_session() as session:
            row = session.get(OpenPosition, position_id)
            if row is None:
                return
            if amount is not None:
                row.amount = amount
            if entry_price is not None:
                row.entry_price = entry_price
            if entry_fee_usdt is not None:
                row.entry_fee_usdt = entry_fee_usdt
            session.add(row)
            session.commit()

    def get_decisions_without_outcome(
            self,
            symbol: Optional[str] = None,
            mode: Optional[str] = None,
            limit: int = 1000,
    ) -> List[Decision]:
        """Return recent decisions that do not have an outcome_label yet.

        This is used by offline labeling jobs to attach PnL / outcome info
        after trades have been closed.
        """
        with self._get_session() as session:
            stmt = select(Decision).where(Decision.bot_type == self.bot_type)
            stmt = stmt.where(Decision.outcome_label.is_(None))
            if symbol is not None:
                stmt = stmt.where(Decision.symbol == symbol)
            if mode is not None:
                stmt = stmt.where(Decision.mode == mode)
            stmt = stmt.order_by(Decision.timestamp.desc()).limit(limit)
            rows = session.exec(stmt).all()
            return list(rows)

    def get_bot_config_value(
            self,
            key: str,
            default: Optional[str] = None,
            bot_type: Optional[str] = None,
    ) -> Optional[str]:
        """
        Returns the configuration value for (bot_type, key) as a string,
        or the provided default if not found.
        """
        effective_bot_type = bot_type or self.bot_type
        with self._get_session() as session:
            stmt = (
                select(BotConfig)
                .where(
                    BotConfig.bot_type == effective_bot_type,
                    BotConfig.key == key,
                )
                .limit(1)
            )
            row = session.exec(stmt).first()
            if row is None:
                return default
            return row.value

    def get_bot_config_float(
            self,
            key: str,
            default: float,
            bot_type: Optional[str] = None,
    ) -> float:
        """
        Returns configuration value cast to float, or default on error / missing.
        """
        raw = self.get_bot_config_value(key, None, bot_type=bot_type)
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def get_bot_config_int(
            self,
            key: str,
            default: int,
            bot_type: Optional[str] = None,
    ) -> int:
        """
        Returns configuration value cast to int, or default on error / missing.
        """
        raw = self.get_bot_config_value(key, None, bot_type=bot_type)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def get_bot_config_prefix(
        self,
        prefix: str,
        bot_type: Optional[str] = None,
    ) -> dict[str, str]:
        """
        Returns all configuration key/value pairs for this bot_type whose key
        starts with the provided prefix. For example: 'target_weight.'.
        """
        effective_bot_type = bot_type or self.bot_type
        with self._get_session() as session:
            stmt = (
                select(BotConfig)
                .where(BotConfig.bot_type == effective_bot_type)
                .where(BotConfig.key.like(f"{prefix}%"))
            )
            rows = session.exec(stmt).all()
            return {row.key: row.value for row in rows}

    def set_bot_config_value(self, key: str, value: str, bot_type: Optional[str] = None) -> None:
        """
        Creates or updates a configuration value for (bot_type, key).
        """
        effective_bot_type = bot_type or self.bot_type
        with self._get_session() as session:
            stmt = (
                select(BotConfig)
                .where(
                    BotConfig.bot_type == effective_bot_type,
                    BotConfig.key == key,
                )
                .limit(1)
            )
            row = session.exec(stmt).first()
            if row is None:
                row = BotConfig(bot_type=effective_bot_type, key=key, value=value)
                session.add(row)
            else:
                row.value = value
            session.commit()

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------
    def close(self) -> None:
        # SQLModel/SQLAlchemy manages the engine lifecycle internally.
        # Method kept for backward compatibility with existing code.
        pass
