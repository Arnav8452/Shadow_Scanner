"""Cockpit Strategies view, event-arbitrage panel and swing panel (read-only research API)."""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from engine.cockpit import research
from engine.core.config import load_settings
from engine.strategies.registry import REGISTRY, STATUSES


def test_registry_covers_every_spec_strategy_and_addendum_event(repo_root: Path) -> None:
    spec = (repo_root / "docs" / "SPEC.md").read_text(encoding="utf-8")
    sections = set(re.findall(r"^### (5\.\d+) ", spec, flags=re.M))
    assert sections >= {"5.1", "5.8", "5.10"}
    assert sections <= {it.ref for it in REGISTRY}
    addendum = (repo_root / "docs" / "ADDENDUM_EVENT_ARB.md").read_text(encoding="utf-8")
    table = addendum.split("## A.1", 1)[1].split("## A.2", 1)[0]
    events = re.findall(r"^\| \*\*([^*]+)\*\*|^\| ([A-Z][^|]+?) \|", table, flags=re.M)
    names = [(a or b).strip() for a, b in events if (a or b).strip() not in ("Event",)]
    assert len(names) == 7
    a1 = [it.name.lower() for it in REGISTRY if it.ref == "A.1"]
    for n in names:
        key = n.lower().split(" (")[0].split(" & ")[0].split(" /")[0]
        assert any(key[:12] in x for x in a1), n


def test_registry_statuses_are_honest(repo_root: Path) -> None:
    assert {it.status for it in REGISTRY} <= set(STATUSES)
    settings = load_settings(root=repo_root, environ={})
    if not settings.raw["execution"]["live_enabled"]:
        assert not [it for it in REGISTRY if it.status == "live"]
    for it in REGISTRY:
        if it.status == "paper":
            assert it.evidence, f"{it.name}: a paper strategy names the files that show it ran"


def test_strategies_view_reports_last_output(tmp_path: Path) -> None:
    d = tmp_path / "data" / "state" / "swing"
    d.mkdir(parents=True)
    (d / "2026-11-11.json").write_text("{}", encoding="utf-8")
    view = research.strategies(tmp_path)
    swing = [r for r in view["rows"] if r["ref"] == "5.8"]
    assert swing and all(r["last_output"] for r in swing)
    assert all(r["last_output"] is None for r in view["rows"] if r["ref"] == "5.1")
    assert sum(view["counts"].values()) == len(REGISTRY)


def test_swing_view_without_runs(tmp_path: Path) -> None:
    view = research.swing(tmp_path)
    assert "empty" in view and view["days"] == []


def test_event_arb_screen_has_no_side_effects(repo_root: Path, tmp_path: Path) -> None:
    shutil.copytree(repo_root / "config", tmp_path / "config")
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    view = research.event_arb(tmp_path, as_of=date(2026, 11, 12))
    after = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    assert after == before  # no report, alert state or candidate file written
    assert view["reports"] == [] and view["candidates"] == []
    assert isinstance(view["rows"], list)
    for r in view["rows"]:
        assert r["verdict"] in ("GREEN", "AMBER", "RED")


def test_event_arb_report_reads_only_dated_files(tmp_path: Path) -> None:
    rep = tmp_path / "reports" / "event_arb"
    rep.mkdir(parents=True)
    (rep / "2026-11-11.html").write_text("<p>ok</p>", encoding="utf-8")
    assert research.event_arb_report(tmp_path, "2026-11-11")["html"] == "<p>ok</p>"
    for bad in ("../../config/settings", "2026-11-11.html", ""):
        with pytest.raises(ValueError):
            research.event_arb_report(tmp_path, bad)
    with pytest.raises(FileNotFoundError):
        research.event_arb_report(tmp_path, "2026-11-10")


def test_gateway_serves_the_new_panels(repo_root: Path, tmp_path: Path) -> None:
    import asyncio
    import socket

    from aiohttp.test_utils import TestClient, TestServer

    from engine.cockpit.config import CockpitConfig
    from engine.cockpit.gateway import Gateway

    def free() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    port = free()
    ccfg = CockpitConfig(
        gateway_port=port, engine_port=free(), replay_port=free(),
        key_file=tmp_path / "cockpit.key", journal_dir=tmp_path, replay_dir=tmp_path / "replay",
    )  # fmt: skip
    gw = Gateway(ccfg, repo_root, ledger_file=tmp_path / "ledger.jsonl")
    h = {"X-Cockpit-Token": gw.token}

    async def run() -> None:
        async with TestClient(TestServer(gw.app(), host="127.0.0.1", port=port)) as c:
            r = await c.get("/api/research/strategies")
            assert r.status == 403
            r = await c.get("/api/research/strategies", headers=h)
            assert r.status == 200 and len((await r.json())["rows"]) == len(REGISTRY)
            r = await c.get("/api/research/swing", headers=h)
            assert r.status == 200
            r = await c.get("/api/research/event_arb_report?name=..%2Fx", headers=h)
            assert r.status == 404
            page = await (await c.get("/")).text()
            assert all(x in page for x in ("rs-strat", "rs-swing", "rs-earb"))

    asyncio.run(run())
