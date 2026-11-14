from __future__ import annotations

import dataclasses

import pytest

from face_match import __version__
from face_match.cli import check_auth, main
from face_match.config import Settings


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_open_api_is_allowed_on_loopback(host):
    check_auth(Settings(), host)


def test_open_api_on_a_network_interface_is_refused():
    with pytest.raises(SystemExit, match="API_KEY"):
        check_auth(Settings(), "0.0.0.0")


def test_network_interface_is_fine_with_a_key_or_explicit_opt_out():
    check_auth(dataclasses.replace(Settings(), api_key="secret"), "0.0.0.0")
    check_auth(dataclasses.replace(Settings(), allow_no_auth=True), "0.0.0.0")


def test_allow_no_auth_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("ALLOW_NO_AUTH", "true")
    assert Settings.from_env().allow_no_auth is True


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_import_subcommand_passes_its_arguments_through(tmp_path, capsys):
    (tmp_path / "EMP-1_Asha Verma.jpg").write_bytes(b"not checked in a dry run")
    with pytest.raises(SystemExit) as exit_info:
        main(["import", "--dry-run", "--photos", str(tmp_path), "--report", str(tmp_path / "r.csv")])
    assert exit_info.value.code == 0
    assert "valid" in capsys.readouterr().out
