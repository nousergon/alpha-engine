"""Unit tests for executor/ibkr.py — IB Gateway wrapper helpers.

IB connection logic is mocked; these tests cover the parsing layer only.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from executor.ibkr import IBKRClient


def _client_with_account_values(values):
    """Build an IBKRClient with a mocked ib.accountValues() response."""
    client = IBKRClient.__new__(IBKRClient)
    client.ib = MagicMock()
    client.ib.isConnected.return_value = True
    client.ib.accountValues.return_value = values
    return client


class TestAccruedDividendsBySymbol:
    def test_empty_account_values(self):
        client = _client_with_account_values([])
        assert client.get_accrued_dividends_by_symbol() == {}

    def test_parses_per_symbol_accruals(self):
        client = _client_with_account_values([
            SimpleNamespace(tag="AccruedDividend", value="12.50", currency="USD", modelCode="AAPL", account="DU123"),
            SimpleNamespace(tag="DividendAccruals", value="7.25", currency="USD", modelCode="MSFT", account="DU123"),
            # No modelCode — a total-level entry, must be ignored here
            SimpleNamespace(tag="AccruedDividend", value="19.75", currency="USD", modelCode="", account="DU123"),
            # Unrelated tag — must be ignored
            SimpleNamespace(tag="NetLiquidation", value="100000", currency="USD", modelCode="", account="DU123"),
        ])
        result = client.get_accrued_dividends_by_symbol()
        assert result == {"AAPL": 12.50, "MSFT": 7.25}

    def test_skips_zero_and_non_numeric(self):
        client = _client_with_account_values([
            SimpleNamespace(tag="AccruedDividend", value="0", currency="USD", modelCode="AAPL", account="DU"),
            SimpleNamespace(tag="AccruedDividend", value="not-a-number", currency="USD", modelCode="MSFT", account="DU"),
            SimpleNamespace(tag="AccruedDividend", value="5.00", currency="USD", modelCode="GOOG", account="DU"),
        ])
        result = client.get_accrued_dividends_by_symbol()
        assert result == {"GOOG": 5.00}

    def test_sums_multiple_entries_for_same_symbol(self):
        """IB sometimes splits a symbol across multiple AccountValue rows."""
        client = _client_with_account_values([
            SimpleNamespace(tag="AccruedDividend", value="3.00", currency="USD", modelCode="AAPL", account="DU"),
            SimpleNamespace(tag="DividendAccruals", value="4.50", currency="USD", modelCode="AAPL", account="DU"),
        ])
        result = client.get_accrued_dividends_by_symbol()
        assert result == {"AAPL": 7.50}
