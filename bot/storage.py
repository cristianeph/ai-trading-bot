# src/storage.py

import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional

DB_PATH = Path("data") / "trading.db"


class Storage:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()

        cur.execute("""
                    CREATE TABLE IF NOT EXISTS trades
                    (
                        id
                        INTEGER
                        PRIMARY
                        KEY
                        AUTOINCREMENT,
                        timestamp
                        TEXT,
                        symbol
                        TEXT,
                        side
                        TEXT,
                        price
                        REAL,
                        amount
                        REAL,
                        mode
                        TEXT, -- paper / live
                        pnl
                        REAL
                    )
                    """)

        cur.execute("""
                    CREATE TABLE IF NOT EXISTS equity
                    (
                        id
                        INTEGER
                        PRIMARY
                        KEY
                        AUTOINCREMENT,
                        timestamp
                        TEXT,
                        equity
                        REAL
                    )
                    """)

        self.conn.commit()

    def log_trade(self, symbol: str, side: str, price: float, amount: float,
                  mode: str = "paper", pnl: Optional[float] = None):
        ts = datetime.utcnow().isoformat()
        cur = self.conn.cursor()
        cur.execute("""
                    INSERT INTO trades (timestamp, symbol, side, price, amount, mode, pnl)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (ts, symbol, side, price, amount, mode, pnl))
        self.conn.commit()

    def log_equity(self, equity: float):
        ts = datetime.utcnow().isoformat()
        cur = self.conn.cursor()
        cur.execute("""
                    INSERT INTO equity (timestamp, equity)
                    VALUES (?, ?)
                    """, (ts, equity))
        self.conn.commit()

    def get_equity_curve(self):
        cur = self.conn.cursor()
        cur.execute("SELECT timestamp, equity FROM equity ORDER BY timestamp ASC")
        rows = cur.fetchall()
        return rows

    def close(self):
        self.conn.close()
