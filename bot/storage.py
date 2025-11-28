from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple

from sqlmodel import Field, SQLModel, create_engine, Session, select

DB_PATH = Path("data") / "trading.db"


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
    Pequeño wrapper de almacenamiento usando SQLModel (ORM sobre SQLite).

    - Usa data/trading.db como base de datos por defecto.
    - Expone la misma interfaz pública que la versión anterior:
      - log_trade(...)
      - log_equity(...)
      - get_equity_curve()
      - close()
    """

    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        # Crear tablas si no existen
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
        # Con SQLModel/SQLAlchemy, el engine se gestiona internamente.
        # Dejamos el método por compatibilidad con el código existente.
        pass