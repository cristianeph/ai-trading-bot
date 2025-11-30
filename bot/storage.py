import os

from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple

from sqlalchemy import desc
from sqlmodel import Field, SQLModel, create_engine, Session, select

# Default path used for SQLite fallback (when no MySQL env vars are provided)
DB_PATH = Path("data") / "trading.db"

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


class Equity(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: str = Field(index=True)
    equity: float


class Decision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: str = Field(index=True)
    symbol: str
    action: str            # "buy" / "sell" / "hold"
    confidence: float
    ma_ratio: float
    rsi_14: float
    vol_20: float
    mode: str              # "paper" / "live"
    equity_before: Optional[float] = None
    price: Optional[float] = None
    candle_ts: Optional[str] = None


class Storage:
    """
    Simple storage wrapper using SQLModel (ORM over SQLite or MySQL).
    - Uses SQLite by default unless MYSQL_* environment variables are supplied.
    - Public methods:
        - log_trade(...)
        - log_equity(...)
        - get_equity_curve()
        - close()
    """

    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        db_url = get_db_url()
        print("Db engine detected:", db_url)
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        self.engine = create_engine(
            db_url,
            echo=False,
            connect_args=connect_args,
        )
        # Create tables if they do not exist
        SQLModel.metadata.create_all(self.engine)

    def _get_session(self) -> Session:
        return Session(self.engine)

    def log_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        amount: float,
        mode: str = "paper",
        pnl: Optional[float] = None,
    ) -> None:
        ts = datetime.utcnow().isoformat()
        trade = Trade(
            timestamp=ts,
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
            mode=mode,
            pnl=pnl,
        )
        with self._get_session() as session:
            session.add(trade)
            session.commit()

    def get_last_buy(self, symbol: str, mode: str = "live"):
        """
        Returns the most recent BUY trade for the given symbol and trading mode,
        or None if there is no record.
        """
        with Session(self.engine) as session:
            statement = (
                select(Trade)
                .where(
                    Trade.symbol == symbol,
                    Trade.side == "buy",
                    Trade.mode == mode,
                )
                .order_by(desc(Trade.timestamp))
                .limit(1)
            )
            result = session.exec(statement) # type: ignore[arg-type]
            return result.first()

    def log_equity(self, equity: float) -> None:
        ts = datetime.utcnow().isoformat()
        equity_row = Equity(timestamp=ts, equity=equity)
        with self._get_session() as session:
            session.add(equity_row)
            session.commit()

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
        """
        ts = datetime.utcnow().isoformat()
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
        )
        with self._get_session() as session:
            session.add(decision)
            session.commit()

    def get_equity_curve(self) -> List[Tuple[str, float]]:
        with self._get_session() as session:
            stmt = select(Equity).order_by(Equity.timestamp.asc())
            rows = session.exec(stmt).all()
            return [(row.timestamp, row.equity) for row in rows]

    def close(self) -> None:
        # SQLModel/SQLAlchemy manages the engine lifecycle internally.
        # Method kept for backward compatibility with existing code.
        pass