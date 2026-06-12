from echotwin.logging_setup import sanitize


def test_sanitize_bearer_token():
    s = "Authorization: Bearer abc123-xyz_456"
    out = sanitize(s)
    assert "abc123" not in out
    assert "Bearer ***" in out


def test_sanitize_anthropic_key():
    s = "key=sk-ant-api03-very-secret-key-here"
    out = sanitize(s)
    assert "sk-ant-api03" not in out


def test_sanitize_env_var_assignment():
    s = "DISCORD_TOKEN=mysecret123"
    out = sanitize(s)
    assert "mysecret123" not in out


def test_sanitize_plain_text_unchanged():
    assert sanitize("Hello, world!") == "Hello, world!"


def test_file_sink_writes_and_sanitizes(tmp_path):
    """Logs are written to disk, and the file content is sanitized too."""
    from echotwin.logging_setup import setup_logging
    from loguru import logger
    import glob

    setup_logging(log_dir=str(tmp_path))
    logger.info("Bearer abc123secret and normal text")
    logger.complete()
    files = glob.glob(str(tmp_path / "*.log"))
    assert files, "必须产生日志文件"
    content = open(files[0], encoding="utf-8").read()
    assert "Bearer ***" in content and "abc123secret" not in content
    assert "normal text" in content
    # Restore the default setup so other tests are unaffected
    setup_logging(log_dir=str(tmp_path))
