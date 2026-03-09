"""
Thin ib_insync wrapper for the 4 operations the executor needs:
  - get_portfolio_nav()
  - get_positions()
  - get_current_price(ticker)
  - place_market_order(ticker, action, shares)
  - get_historical_bar(ticker, date)   ← used by backtester price_loader fallback

IB Gateway must be running locally on port 4002 in paper mode.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

from ib_insync import IB, Stock, MarketOrder

logger = logging.getLogger(__name__)


class IBKRClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 1):
        self.ib = IB()
        logger.info(f"Connecting to IB Gateway at {host}:{port} (clientId={client_id})")
        self.ib.connect(host, port, clientId=client_id, timeout=20)
        if not self.ib.isConnected():
            raise RuntimeError("Failed to connect to IB Gateway")
        logger.info("Connected to IB Gateway")

    # ── Account ───────────────────────────────────────────────────────────────

    def get_portfolio_nav(self) -> float:
        """Return current Net Liquidation Value from account summary."""
        summary = {s.tag: s for s in self.ib.accountSummary()}
        nav = float(summary["NetLiquidation"].value)
        logger.info(f"Portfolio NAV: ${nav:,.2f}")
        return nav

    def get_positions(self) -> dict[str, dict]:
        """
        Return current portfolio positions.

        Returns:
            {ticker: {"shares": int, "market_value": float, "avg_cost": float, "sector": str}}
        """
        positions = {}
        for p in self.ib.portfolio():
            ticker = p.contract.symbol
            positions[ticker] = {
                "shares": int(p.position),
                "market_value": float(p.marketValue),
                "avg_cost": float(p.averageCost),
                "sector": "",  # sector populated from signals.json by caller
            }
        logger.info(f"Open positions: {len(positions)}")
        return positions

    # ── Market data ───────────────────────────────────────────────────────────

    def get_current_price(self, ticker: str) -> float | None:
        """
        Fetch last trade price for ticker.
        Returns None if no price available (pre-market, bad contract, etc.).
        """
        contract = Stock(ticker, "SMART", "USD")
        try:
            self.ib.qualifyContracts(contract)
        except Exception as e:
            logger.warning(f"Could not qualify contract for {ticker}: {e}")
            return None

        ticker_data = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(1)

        price = ticker_data.last or ticker_data.close
        if not price or price <= 0 or not math.isfinite(price):
            logger.warning(f"No valid price for {ticker} (last={ticker_data.last} close={ticker_data.close})")
            return None

        return float(price)

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_market_order(self, ticker: str, action: str, shares: int) -> dict:
        """
        Place a market order.

        Args:
            ticker: stock symbol
            action: "BUY" | "SELL"
            shares: number of shares

        Returns:
            {"ib_order_id": int, "status": str}
        """
        contract = Stock(ticker, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        order = MarketOrder(action, shares)
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)

        logger.info(
            f"Order placed: {action} {shares} {ticker} "
            f"| orderId={trade.order.orderId} status={trade.orderStatus.status}"
        )
        return {
            "ib_order_id": trade.order.orderId,
            "status": trade.orderStatus.status,
        }

    # ── Historical data ───────────────────────────────────────────────────────

    def get_historical_bar(self, ticker: str, date: str) -> dict | None:
        """
        Fetch a single daily OHLCV bar for ticker on date (YYYY-MM-DD).

        Used by the backtester's price_loader as a fallback when prices.json
        is missing from S3 and yfinance returns no data for a ticker.

        Returns:
            {"open": float, "close": float, "high": float, "low": float}
            or None if no data available.

        Note: IBKR rate-limits historical data requests (~50 req/10s on paper).
        The backtester only calls this for tickers yfinance missed, so volume
        should be low in practice.
        """
        contract = Stock(ticker, "SMART", "USD")
        try:
            self.ib.qualifyContracts(contract)
        except Exception as e:
            logger.warning(f"Could not qualify contract for {ticker}: {e}")
            return None

        # reqHistoricalData endDateTime format: "YYYYMMDD HH:MM:SS"
        end_dt = datetime.strptime(date, "%Y-%m-%d").strftime("%Y%m%d 23:59:59")
        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime=end_dt,
                durationStr="1 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        except Exception as e:
            logger.warning(f"reqHistoricalData failed for {ticker} on {date}: {e}")
            return None

        if not bars:
            logger.debug(f"No historical bar for {ticker} on {date} (weekend/holiday?)")
            return None

        bar = bars[-1]
        return {
            "open":  float(bar.open),
            "close": float(bar.close),
            "high":  float(bar.high),
            "low":   float(bar.low),
        }

    # ── Peak NAV ──────────────────────────────────────────────────────────────

    def get_peak_nav(self, db_conn) -> float:
        """
        Return the highest portfolio NAV recorded in trades.db.
        Used for drawdown circuit breaker.
        Falls back to current NAV if no history.
        """
        row = db_conn.execute(
            "SELECT MAX(portfolio_nav_at_order) FROM trades"
        ).fetchone()
        if row and row[0]:
            return float(row[0])
        return self.get_portfolio_nav()

    def disconnect(self):
        self.ib.disconnect()
        logger.info("Disconnected from IB Gateway")


class SimulatedIBKRClient:
    """Drop-in replacement for IBKRClient used in backtesting.
    Reads prices from a pre-loaded dict; never connects to IB Gateway."""

    def __init__(self, prices: dict[str, float], nav: float = 1_000_000.0):
        self._prices = prices      # {ticker: price}
        self._nav = nav
        self._positions: dict = {}

    def get_portfolio_nav(self) -> float:
        return self._nav

    def get_positions(self) -> dict:
        return self._positions

    def get_peak_nav(self, conn) -> float:
        return self._nav

    def get_current_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def place_market_order(self, ticker: str, action: str, shares: int) -> dict:
        # Record fill for portfolio tracking; return stub order ID
        price = self._prices.get(ticker, 0)
        if action == "BUY":
            self._positions[ticker] = {"shares": shares, "avg_cost": price}
            self._nav -= shares * price
        elif action == "SELL":
            self._positions.pop(ticker, None)
            self._nav += shares * price
        return {"ib_order_id": None}

    def get_historical_bar(self, ticker: str, date: str) -> dict | None:
        # SimulatedIBKRClient only holds a single close price per ticker.
        # Return it for all OHLCV fields — sufficient for backtester fallback use.
        price = self._prices.get(ticker)
        if price is None:
            return None
        return {"open": price, "close": price, "high": price, "low": price}

    def disconnect(self):
        pass
