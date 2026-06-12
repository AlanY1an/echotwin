import pytest

from echotwin.wake_word.fast_response import FastResponseCache


@pytest.fixture
def cache(tmp_path):
    return FastResponseCache(
        persona_id="test",
        voice_id="vid",
        responses=["嗯?", "在的"],
        data_dir=tmp_path,
    )


def test_filename_for_text_is_stable(cache):
    f1 = cache._path_for("嗯?")
    f2 = cache._path_for("嗯?")
    assert f1 == f2
    assert f1.parent.name == "test"


@pytest.mark.asyncio
async def test_ensure_synthesizes_missing(cache):
    calls = []

    async def fake_synth(text: str) -> bytes:
        calls.append(text)
        return f"audio:{text}".encode()

    await cache.ensure_synthesized(synth_fn=fake_synth)
    assert sorted(calls) == sorted(["嗯?", "在的"])
    assert cache._path_for("嗯?").exists()
    assert cache._path_for("在的").exists()


@pytest.mark.asyncio
async def test_ensure_skips_existing(cache):
    p = cache._path_for("嗯?")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"already there")

    calls = []

    async def fake_synth(text: str) -> bytes:
        calls.append(text)
        return f"audio:{text}".encode()

    await cache.ensure_synthesized(synth_fn=fake_synth)
    assert calls == ["在的"]
    assert cache._path_for("嗯?").read_bytes() == b"already there"


@pytest.mark.asyncio
async def test_ensure_removes_stale(cache):
    cache._dir.mkdir(parents=True, exist_ok=True)
    stale = cache._dir / "deadbeef.ogg"
    stale.write_bytes(b"old")

    async def fake_synth(text: str) -> bytes:
        return f"a:{text}".encode()

    await cache.ensure_synthesized(synth_fn=fake_synth)
    assert not stale.exists()


@pytest.mark.asyncio
async def test_get_random_returns_existing(cache):
    p = cache._path_for("嗯?")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    p2 = cache._path_for("在的")
    p2.write_bytes(b"y")
    chosen = await cache.get_random()
    assert chosen in {p, p2}


@pytest.mark.asyncio
async def test_get_random_returns_none_when_empty(cache):
    assert (await cache.get_random()) is None


@pytest.mark.asyncio
async def test_synth_failure_doesnt_crash(cache):
    async def bad_synth(text: str) -> bytes:
        raise RuntimeError("network down")

    await cache.ensure_synthesized(synth_fn=bad_synth)
    # No file should exist for either response
    assert (await cache.get_random()) is None


@pytest.mark.asyncio
async def test_cleanup_preserves_underscore_reserved_files(cache):
    """Underscore-prefixed files (e.g. _limit.ogg, the quota announcement) are reserved and
    must survive cleanup — deleting them by mistake means a paid re-synthesis on every
    startup / SIGHUP / persona switch."""
    cache.dir.mkdir(parents=True, exist_ok=True)
    limit = cache.dir / "_limit.ogg"
    limit.write_bytes(b"cached limit audio")
    stale = cache.dir / "deadbeef0000.ogg"
    stale.write_bytes(b"stale")

    async def synth(text: str) -> bytes:
        return b"audio"

    await cache.ensure_synthesized(synth_fn=synth)

    assert limit.exists(), "_limit.ogg 被 warm-up 清理误删"
    assert not stale.exists(), "真正的 stale 文件应该被清掉"
