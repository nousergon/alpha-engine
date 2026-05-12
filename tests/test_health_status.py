"""Unit tests for executor.health_status — freshness + write/read utilities."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from executor.health_status import (
    check_upstream_health,
    read_health,
    verify_s3_object_fresh,
    write_data_manifest,
    write_health,
)


def _s3_mock(last_modified=None, raise_exc=None):
    s3 = MagicMock()
    if raise_exc is not None:
        s3.head_object.side_effect = raise_exc
    else:
        s3.head_object.return_value = {"LastModified": last_modified}
    return s3


class TestVerifyS3ObjectFresh:

    def test_missing_object_raises_runtime_error(self):
        s3 = _s3_mock(raise_exc=Exception("NoSuchKey"))
        with pytest.raises(RuntimeError, match="not found"):
            verify_s3_object_fresh(s3, "bucket", "key", "2026-04-16")

    def test_fresh_today_passes(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        s3 = _s3_mock(last_modified=recent)
        verify_s3_object_fresh(s3, "bucket", "key", today)

    def test_stale_today_raises_runtime_error(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday_write = datetime.now(timezone.utc) - timedelta(hours=25)
        s3 = _s3_mock(last_modified=yesterday_write)
        with pytest.raises(RuntimeError, match="stale"):
            verify_s3_object_fresh(s3, "bucket", "key", today)

    def test_backfill_skips_freshness_check(self):
        """Historical run_date: existence-only, LastModified irrelevant."""
        old_write = datetime.now(timezone.utc) - timedelta(days=30)
        s3 = _s3_mock(last_modified=old_write)
        verify_s3_object_fresh(s3, "bucket", "key", "2026-03-10")

    def test_just_under_threshold_today_passes(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        almost_stale = datetime.now(timezone.utc) - timedelta(hours=11, minutes=59)
        s3 = _s3_mock(last_modified=almost_stale)
        verify_s3_object_fresh(s3, "bucket", "key", today)

    def test_custom_max_age_hours(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        three_hours_old = datetime.now(timezone.utc) - timedelta(hours=3)
        s3 = _s3_mock(last_modified=three_hours_old)
        verify_s3_object_fresh(s3, "bucket", "key", today, max_age_hours=6)
        with pytest.raises(RuntimeError, match="stale"):
            verify_s3_object_fresh(s3, "bucket", "key", today, max_age_hours=2)


class TestWriteHealth:
    """Covers `write_health` — S3 put + error path."""

    def test_writes_status_ok_with_summary(self):
        s3 = MagicMock()
        with patch("executor.health_status.boto3.client", return_value=s3):
            write_health(
                bucket="b", module_name="executor", status="ok",
                run_date="2026-05-12", duration_seconds=12.345,
                summary={"n_orders": 3}, warnings=["minor"],
            )
        s3.put_object.assert_called_once()
        call = s3.put_object.call_args
        assert call.kwargs["Bucket"] == "b"
        assert call.kwargs["Key"] == "health/executor.json"
        payload = json.loads(call.kwargs["Body"])
        assert payload["module"] == "executor"
        assert payload["status"] == "ok"
        assert payload["last_success"] is not None
        assert payload["run_date"] == "2026-05-12"
        assert payload["duration_seconds"] == 12.3  # rounded to 1dp
        assert payload["summary"] == {"n_orders": 3}
        assert payload["warnings"] == ["minor"]
        assert payload["error"] is None

    def test_failed_status_clears_last_success(self):
        s3 = MagicMock()
        with patch("executor.health_status.boto3.client", return_value=s3):
            write_health(
                bucket="b", module_name="executor", status="failed",
                run_date="2026-05-12", duration_seconds=0.5,
                error="connection refused",
            )
        payload = json.loads(s3.put_object.call_args.kwargs["Body"])
        assert payload["status"] == "failed"
        assert payload["last_success"] is None  # cleared on failure
        assert payload["error"] == "connection refused"
        assert payload["summary"] == {}  # default empty
        assert payload["warnings"] == []  # default empty

    def test_put_failure_swallowed_with_warning(self, caplog):
        """S3 failure must not crash the caller — this is best-effort telemetry."""
        s3 = MagicMock()
        s3.put_object.side_effect = Exception("AccessDenied")
        with patch("executor.health_status.boto3.client", return_value=s3):
            write_health(
                bucket="b", module_name="executor", status="ok",
                run_date="2026-05-12", duration_seconds=1.0,
            )
        # No exception escaped


class TestWriteDataManifest:
    """Covers `write_data_manifest` — dated, never-overwritten manifests."""

    def test_writes_dated_manifest(self):
        s3 = MagicMock()
        with patch("executor.health_status.boto3.client", return_value=s3):
            write_data_manifest(
                bucket="b", module_name="executor", run_date="2026-05-12",
                manifest={"trades": 5, "alpha_pct": 0.42},
            )
        call = s3.put_object.call_args
        assert call.kwargs["Key"] == "data_manifest/executor/2026-05-12.json"
        payload = json.loads(call.kwargs["Body"])
        assert payload["module"] == "executor"
        assert payload["run_date"] == "2026-05-12"
        assert "written_at" in payload
        assert payload["trades"] == 5
        assert payload["alpha_pct"] == 0.42

    def test_put_failure_swallowed(self):
        s3 = MagicMock()
        s3.put_object.side_effect = Exception("AccessDenied")
        with patch("executor.health_status.boto3.client", return_value=s3):
            write_data_manifest(
                bucket="b", module_name="executor", run_date="2026-05-12",
                manifest={"trades": 0},
            )
        # No exception escaped


class TestReadHealth:
    """Covers `read_health` — S3 get with graceful None on missing."""

    def test_reads_existing_health(self):
        body = json.dumps({"module": "executor", "status": "ok"}).encode()
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=body))}
        with patch("executor.health_status.boto3.client", return_value=s3):
            result = read_health("b", "executor")
        assert result == {"module": "executor", "status": "ok"}

    def test_missing_returns_none(self):
        s3 = MagicMock()
        s3.get_object.side_effect = Exception("NoSuchKey")
        with patch("executor.health_status.boto3.client", return_value=s3):
            result = read_health("b", "executor")
        assert result is None


class TestCheckUpstreamHealth:
    """Covers `check_upstream_health` — multi-module health aggregation."""

    def _health_with_last_success(self, hours_ago: float, status: str = "ok") -> dict:
        last = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        return {"module": "x", "status": status, "last_success": last}

    def test_all_healthy(self):
        fresh = self._health_with_last_success(hours_ago=2.0)

        def fake_read(bucket, mod):
            return fresh

        with patch("executor.health_status.read_health", side_effect=fake_read):
            result = check_upstream_health("b", ["research", "predictor"])

        assert set(result.keys()) == {"research", "predictor"}
        for mod in ("research", "predictor"):
            assert result[mod]["status"] == "ok"
            assert result[mod]["stale"] is False
            assert 1.5 < result[mod]["age_hours"] < 2.5

    def test_missing_module_marked_unknown_and_stale(self):
        with patch("executor.health_status.read_health", return_value=None):
            result = check_upstream_health("b", ["never_ran"])
        assert result["never_ran"]["status"] == "unknown"
        assert result["never_ran"]["age_hours"] == -1
        assert result["never_ran"]["stale"] is True

    def test_stale_module_detected(self):
        stale = self._health_with_last_success(hours_ago=72.0)  # past default 48h
        with patch("executor.health_status.read_health", return_value=stale):
            result = check_upstream_health("b", ["research"])
        assert result["research"]["status"] == "ok"  # status field unchanged
        assert result["research"]["stale"] is True   # but flagged stale by age
        assert result["research"]["age_hours"] > 48

    def test_custom_max_age_hours(self):
        moderately_old = self._health_with_last_success(hours_ago=10.0)
        with patch("executor.health_status.read_health", return_value=moderately_old):
            result = check_upstream_health("b", ["research"], max_age_hours=8)
        assert result["research"]["stale"] is True

        with patch("executor.health_status.read_health", return_value=moderately_old):
            result = check_upstream_health("b", ["research"], max_age_hours=24)
        assert result["research"]["stale"] is False

    def test_malformed_last_success_age_minus_one(self):
        broken = {"module": "x", "status": "ok", "last_success": "not-a-date"}
        with patch("executor.health_status.read_health", return_value=broken):
            result = check_upstream_health("b", ["research"])
        # ValueError caught — age stays at -1 → stale (age < 0 branch)
        assert result["research"]["age_hours"] == -1
        assert result["research"]["stale"] is True

    def test_missing_last_success_field(self):
        no_ts = {"module": "x", "status": "ok"}  # no last_success
        with patch("executor.health_status.read_health", return_value=no_ts):
            result = check_upstream_health("b", ["research"])
        assert result["research"]["age_hours"] == -1
        assert result["research"]["stale"] is True

    def test_status_unknown_when_missing(self):
        no_status = {"module": "x", "last_success": datetime.now(timezone.utc).isoformat()}
        with patch("executor.health_status.read_health", return_value=no_status):
            result = check_upstream_health("b", ["research"])
        assert result["research"]["status"] == "unknown"  # falls through to default
