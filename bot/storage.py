from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple
import os

from sqlmodel import Field, SQLModel, create_engine, Session, select

# Default path used for SQLite fallback (when no MySQL env vars are provided)
DB_PATH = Path("data") / "trading.db"

def get_db_url() -> str:
    """
    Returns the database connection URL for SQLModel/SQLAlchemy.
    - If environment variables MYSQL_HOST are present
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

    def close(self) -> None:
        # SQLModel/SQLAlchemy manages the engine lifecycle internally.
        # Method kept for backward compatibility with existing code.
        pass