import logging
import subprocess
from pathlib import Path

import pytest

from engine.core.env import Secret, get_secret, load_dotenv, parse_dotenv
from engine.core.logging_setup import MASK, RedactingFilter, redact, setup_logging


def test_parse_dotenv() -> None:
    text = "# c\nMODE=paid\nexport A='x y'\nB=\"q\"\nC=v # trailing\nnoequals\n\n"
    assert parse_dotenv(text) == {"MODE": "paid", "A": "x y", "B": "q", "C": "v"}


def test_load_dotenv_does_not_override(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("MODE=paid\nNEW=1\n", encoding="utf-8")
    env = {"MODE": "free"}
    load_dotenv(p, env)
    assert env == {"MODE": "free", "NEW": "1"}


def test_secret_is_masked() -> None:
    s = get_secret("FYERS_SECRET_KEY", {"FYERS_SECRET_KEY": "abc123supersecret"})
    assert "abc123" not in repr(s) and "abc123" not in str(s) and "abc123" not in f"{s}"
    assert s.reveal() == "abc123supersecret"
    assert not Secret("X", "")


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer abcdefghijklmnop",
        "url?access_token=AbCdEf123456&x=1",
        "api_key: 'k3y-value-123'",
        "got eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIz",
    ],
)
def test_redact_patterns(raw: str) -> None:
    out = redact(raw)
    assert MASK in out
    for secret in ("abcdefghijklmnop", "AbCdEf123456", "k3y-value-123", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in out


def test_redact_env_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:telegram-secret-value")
    assert "telegram-secret-value" not in redact("sending with 999999:telegram-secret-value now")


def test_filter_masks_args_and_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "dhan-token-xyz-0001")
    rec = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "login with %s", ("dhan-token-xyz-0001",), None
    )
    RedactingFilter().filter(rec)
    assert "dhan-token-xyz-0001" not in rec.getMessage()
    try:
        raise RuntimeError("failed token=dhan-token-xyz-0001")
    except RuntimeError:
        import sys

        rec2 = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    RedactingFilter().filter(rec2)
    assert rec2.exc_text and "dhan-token-xyz-0001" not in rec2.exc_text


def test_setup_logging_writes_redacted_file(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FYERS_SECRET_KEY", "fyers-very-secret-1")
    setup_logging(repo_root / "ops" / "logging.yaml", log_dir=tmp_path)
    try:
        logging.getLogger("engine.test").info("secret is %s", "fyers-very-secret-1")
        for h in logging.getLogger().handlers:
            h.flush()
        text = (tmp_path / "engine.log").read_text(encoding="utf-8")
        assert "secret is ****" in text and "fyers-very-secret-1" not in text
    finally:
        for h in list(logging.getLogger().handlers):
            h.close()
            logging.getLogger().removeHandler(h)


# ---------- repo hygiene: secrets and private data never tracked ----------


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", "data/private/report.pdf", "data/parquet/x.parquet", "logs/engine.log"],
)
def test_gitignore_excludes(repo_root: Path, path: str) -> None:
    try:
        res = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=repo_root, check=False, capture_output=True
        )
    except FileNotFoundError:
        pytest.skip("git not installed")
    if res.returncode == 128:
        pytest.skip("not a git checkout")
    assert res.returncode == 0, f"{path} is NOT ignored"


def test_env_example_is_tracked_and_has_no_values(repo_root: Path) -> None:
    res = subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    assert res.returncode != 0, ".env.example must not be ignored"
    for k, v in parse_dotenv((repo_root / ".env.example").read_text(encoding="utf-8")).items():
        assert k == "MODE" or v == "", f"{k} has a value in .env.example"
