import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from engine.core.config import ConfigError, Mode, deep_merge, live_gate, load_settings


@pytest.fixture
def cfg_root(tmp_path: Path, repo_root: Path) -> Path:
    shutil.copytree(repo_root / "config", tmp_path / "config")
    return tmp_path


def patch_yaml(path: Path, **updates: object) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for dotted, value in updates.items():
        node = doc
        *parents, leaf = dotted.split("__")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_default_mode_is_free_and_paper(repo_root: Path) -> None:
    s = load_settings(root=repo_root, environ={})
    assert s.mode is Mode.FREE
    assert s.execution.live_enabled is False
    assert s.execution.broker == "paper"
    assert s.data_primary == "upstox"
    assert s.execution.max_orders_per_second < 10
    assert "MARKET" not in s.execution.allowed_order_types
    assert s.timezone == "Asia/Kolkata"


def test_mode_from_env_and_merge(repo_root: Path) -> None:
    s = load_settings(root=repo_root, environ={"MODE": "paid"})
    assert s.mode is Mode.PAID
    assert s.data_primary == "dhan"
    assert s.risk.max_lots_per_strategy == 1  # inherited from settings.yaml


def test_bad_mode(repo_root: Path) -> None:
    with pytest.raises(ConfigError, match="MODE"):
        load_settings(root=repo_root, environ={"MODE": "yolo"})


def test_deep_merge() -> None:
    assert deep_merge({"a": {"b": 1, "c": 2}, "x": 1}, {"a": {"c": 3}}) == {
        "a": {"b": 1, "c": 3},
        "x": 1,
    }


@pytest.mark.parametrize(
    ("updates", "msg"),
    [
        ({"execution__max_orders_per_second": 10}, "< 10"),
        ({"execution__allowed_order_types": ["LIMIT", "MARKET"]}, "MARKET"),
        ({"execution__allowed_order_types": ["STOP"]}, "subset"),
        ({"execution__reconcile_interval_s": 120}, "30–60"),
        ({"risk__daily_loss_limit_pct": 0}, "> 0"),
    ],
)
def test_hard_rules_enforced(cfg_root: Path, updates: dict[str, object], msg: str) -> None:
    patch_yaml(cfg_root / "config" / "settings.yaml", **updates)
    with pytest.raises(ConfigError, match=msg):
        load_settings(root=cfg_root, environ={})


def test_live_enabled_requires_paid(cfg_root: Path) -> None:
    patch_yaml(cfg_root / "config" / "modes" / "free.yaml", execution__live_enabled=True)
    with pytest.raises(ConfigError, match="MODE=paid"):
        load_settings(root=cfg_root, environ={})


def test_live_gate_blocks_by_default(repo_root: Path) -> None:
    s = load_settings(root=repo_root, environ={"MODE": "paid"})
    gate = live_gate(
        s,
        static_ip_whitelisted=True,
        session_2fa_on=date(2026, 11, 9),
        today=date(2026, 11, 9),
        owner_confirmed=True,
    )
    assert not gate.allowed
    assert any("live_enabled" in r for r in gate.reasons)


def test_live_gate_needs_every_condition(cfg_root: Path) -> None:
    patch_yaml(
        cfg_root / "config" / "modes" / "paid.yaml",
        execution__live_enabled=True,
        execution__static_ip="203.0.113.7",
    )
    s = load_settings(root=cfg_root, environ={"MODE": "paid"})
    today = date(2026, 11, 9)
    ok = dict(static_ip_whitelisted=True, session_2fa_on=today, today=today, owner_confirmed=True)
    assert live_gate(s, **ok).allowed  # type: ignore[arg-type]
    for key, bad in [
        ("static_ip_whitelisted", False),
        ("session_2fa_on", date(2026, 11, 8)),
        ("owner_confirmed", False),
    ]:
        gate = live_gate(s, **{**ok, key: bad})  # type: ignore[arg-type]
        assert not gate.allowed and len(gate.reasons) == 1
