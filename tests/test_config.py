from pathlib import Path

import pytest

from virtudoc.config import DATA_DIR_ENV, Config, data_dir


def test_load_reads_known_keys(home):
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        "[chunk]\ntarget_tokens = 100\n\n[server]\nhttp_port = 9000\n",
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.get_int("chunk.target_tokens") == 100
    assert cfg.get_int("server.http_port") == 9000
    assert cfg.get_int("chunk.max_tokens") == 1200


def test_load_without_config_file_uses_defaults(home):
    cfg = Config.load()
    assert cfg.get("db.path") is None
    assert cfg.get_int("chunk.target_tokens") == 600


def test_load_warns_about_unknown_key(home, capsys):
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        "top_level = 1\n\n[chunk]\ntarget_tokens = 100\nbogus = 1\n",
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.get_int("chunk.target_tokens") == 100
    err = capsys.readouterr().err
    assert "unknown configuration key 'chunk.bogus'" in err
    assert "top_level" not in cfg.values


def test_load_uses_explicit_config_dir(tmp_path):
    (tmp_path / "config.toml").write_text("[log]\nlevel = 'DEBUG'\n", encoding="utf-8")
    cfg = Config.load(config_dir=tmp_path)
    assert cfg.get_str("log.level") == "DEBUG"


def test_get_unknown_key_raises():
    with pytest.raises(KeyError, match="unknown configuration key"):
        Config().get("nope.key")


def test_get_bool_accepts_strings():
    cfg = Config()
    cfg.values["index.auto_refresh"] = "yes"
    assert cfg.get_bool("index.auto_refresh") is True
    cfg.values["index.auto_refresh"] = "off"
    assert cfg.get_bool("index.auto_refresh") is False
    cfg.values["index.auto_refresh"] = 0
    assert cfg.get_bool("index.auto_refresh") is False


def test_db_path_honours_configured_value(home):
    cfg = Config()
    cfg.values["db.path"] = "~/custom/index.db"
    assert cfg.db_path == Path("~/custom/index.db").expanduser()


def test_data_dir_default_and_override(tmp_path, monkeypatch):
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert data_dir() == tmp_path / ".virtudoc"
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "custom"))
    assert data_dir() == tmp_path / "custom"
