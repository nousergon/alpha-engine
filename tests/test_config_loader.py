"""Tests for executor.config_loader — risk.yaml resolution with no silent fallback."""

import os

import pytest
import yaml

from executor import config_loader


def test_get_config_path_returns_first_match(tmp_path, monkeypatch):
    candidate = tmp_path / "risk.yaml"
    candidate.write_text("foo: bar\n")
    monkeypatch.setattr(config_loader, "_SEARCH_PATHS", [str(candidate)])

    resolved = config_loader.get_config_path()
    assert os.path.realpath(str(candidate)) == resolved


def test_get_config_path_picks_first_existing(tmp_path, monkeypatch):
    missing = tmp_path / "nope.yaml"
    real = tmp_path / "real.yaml"
    real.write_text("foo: bar\n")
    monkeypatch.setattr(config_loader, "_SEARCH_PATHS", [str(missing), str(real)])

    resolved = config_loader.get_config_path()
    assert resolved == os.path.realpath(str(real))


def test_get_config_path_raises_when_none_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(config_loader, "_SEARCH_PATHS", [
        str(tmp_path / "a.yaml"),
        str(tmp_path / "b.yaml"),
    ])
    with pytest.raises(FileNotFoundError) as exc:
        config_loader.get_config_path()

    msg = str(exc.value)
    assert "risk.yaml not found" in msg
    assert "a.yaml" in msg
    assert "b.yaml" in msg
    # Example template MUST NOT be searched silently
    assert ".example template is intentionally NOT searched" in msg


def test_load_config_returns_parsed_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(yaml.safe_dump({"signals_bucket": "real-bucket", "max_position_pct": 0.05}))
    monkeypatch.setattr(config_loader, "_SEARCH_PATHS", [str(cfg)])

    loaded = config_loader.load_config()
    assert loaded["signals_bucket"] == "real-bucket"
    assert loaded["max_position_pct"] == 0.05


def test_load_config_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config_loader, "_SEARCH_PATHS", [str(tmp_path / "nope.yaml")])
    with pytest.raises(FileNotFoundError):
        config_loader.load_config()
