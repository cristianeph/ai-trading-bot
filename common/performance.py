from typing import List, Optional, Dict
from sqlmodel import select, Session
from common.storage import Trade, Storage

class PerformanceService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def get_trades_for_pnl(self, symbol: Optional[str] = None, mode: str = "paper", reference_id: Optional[str] = None) -> List[Trade]:
        """
        Fetch trades from storage, optionally filtered by symbol or reference_id.
        """
        with Session(self.storage.engine) as session:
            statement = select(Trade).where(Trade.mode == mode)
            if symbol:
                statement = statement.where(Trade.symbol == symbol)
            if reference_id:
                statement = statement.where(Trade.reference_id == reference_id)
            if self.storage.bot_type:
                statement = statement.where(Trade.bot_type == self.storage.bot_type)
            
            results = session.exec(statement)
            return list(results.all())

    def calculate_waep(self, trades: List[Trade]) -> Dict[str, float]:
        """
        Calculate Weighted Average Entry Price (WAEP) per symbol.
        Only considers 'buy' trades for WAEP calculation in this simple version.
        """
        stats = {} # symbol -> {total_amount, total_cost}
        for t in trades:
            if t.side.lower() != 'buy':
                continue
            
            if t.symbol not in stats:
                stats[t.symbol] = {'amount': 0.0, 'cost': 0.0}
            
            stats[t.symbol]['amount'] += t.amount
            stats[t.symbol]['cost'] += (t.amount * t.price)
        
        waep_results = {}
        for symbol, data in stats.items():
            if data['amount'] > 0:
                waep_results[symbol] = data['cost'] / data['amount']
            else:
                waep_results[symbol] = 0.0
        
        return waep_results

    def calculate_realized_pnl(self, trades: List[Trade]) -> float:
        """
        Calculate total realized PnL from a list of trades.
        If trades have the 'pnl' field populated (from modern logging), it sums them.
        Otherwise, it would need a more complex FIFO/LIFO matching (not implemented yet).
        """
        total_pnl = 0.0
        for t in trades:
            if t.pnl is not None:
                total_pnl += t.pnl
        return total_pnl

    def get_summary_by_reference(self, reference_id: str, mode: str = "paper") -> Dict:
        """
        Aggregates trades by reference_id and provides a summary.
        Useful for rebalancing operations where multiple trades are linked.
        """
        trades = self.get_trades_for_pnl(mode=mode, reference_id=reference_id)
        if not trades:
            return {}

        total_invested = sum(t.invested_usdt_equivalent for t in trades if t.invested_usdt_equivalent)
        total_fees = sum(t.fee_usdt for t in trades if t.fee_usdt)
        total_pnl = sum(t.pnl for t in trades if t.pnl)
        
        return {
            "reference_id": reference_id,
            "trade_count": len(trades),
            "total_invested_usdt": total_invested,
            "total_fees_usdt": total_fees,
            "total_realized_pnl_usdt": total_pnl,
            "symbols": list(set(t.symbol for t in trades))
        }
