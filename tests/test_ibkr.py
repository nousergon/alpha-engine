"""Unit tests for executor/ibkr.py — IB Gateway wrapper helpers.

IB connection logic is mocked; these tests cover the parsing layer only.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


class TestInitialConnectRetry:
    """The constructor must retry a transient connect failure, not hard-fail.

    Regression for the 2026-06-05 weekday-SF failure: the morning planner's
    only IB touchpoint is ``IBKRClient.__init__``, which used to do a single
    bare ``connect()``. An IB Gateway ``reqExecutions`` stall mid-handshake
    raised ``TimeoutError`` and nuked the whole pipeline.
    """

    def _fake_ib(self, monkeypatch, connect_side_effect):
        import executor.ibkr as ibkr_mod
        import executor.retry as retry_mod
        fake_ib = MagicMock()
        fake_ib.connect.side_effect = connect_side_effect
        monkeypatch.setattr(ibkr_mod, "IB", lambda: fake_ib)
        monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)  # no real backoff
        return fake_ib

    def test_retries_then_succeeds(self, monkeypatch):
        state = {"connected": False, "calls": 0}

        def connect_side(*_a, **_k):
            state["calls"] += 1
            if state["calls"] == 1:
                raise TimeoutError("reqExecutions stalled mid-handshake")
            state["connected"] = True

        fake_ib = self._fake_ib(monkeypatch, connect_side)
        fake_ib.isConnected.side_effect = lambda: state["connected"]

        client = IBKRClient()  # must not raise

        assert state["calls"] == 2  # one transient failure, one success
        assert client.ib.isConnected()
        # half-open socket / stale clientId cleared before the retry
        assert fake_ib.disconnect.called

    def test_raises_after_exhausting_attempts(self, monkeypatch):
        fake_ib = self._fake_ib(monkeypatch, TimeoutError("gateway down"))
        fake_ib.isConnected.return_value = False

        with pytest.raises(TimeoutError):
            IBKRClient(reconnect_attempts=2)

        assert fake_ib.connect.call_count == 2  # honors reconnect_attempts, then raises loud
