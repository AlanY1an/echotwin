from echotwin.audio.preroll_buffer import PrerollRingBuffer


def test_basic_push_drain():
    buf = PrerollRingBuffer(max_frames=3)
    buf.push(b"\x01\x02")
    buf.push(b"\x03\x04")
    assert buf.drain() == b"\x01\x02\x03\x04"
    # drain clears
    assert buf.drain() == b""


def test_overflow_drops_oldest():
    buf = PrerollRingBuffer(max_frames=2)
    buf.push(b"a")
    buf.push(b"b")
    buf.push(b"c")
    assert buf.drain() == b"bc"


def test_drain_without_push():
    buf = PrerollRingBuffer(max_frames=5)
    assert buf.drain() == b""


def test_multiple_drains_independent():
    buf = PrerollRingBuffer(max_frames=4)
    buf.push(b"1")
    buf.push(b"2")
    assert buf.drain() == b"12"
    buf.push(b"3")
    assert buf.drain() == b"3"


def test_len():
    buf = PrerollRingBuffer(max_frames=3)
    assert len(buf) == 0
    buf.push(b"a")
    buf.push(b"b")
    assert len(buf) == 2
    buf.push(b"c")
    buf.push(b"d")  # ringbuffer overflow drops oldest
    assert len(buf) == 3


def test_zero_max_frames_safe():
    buf = PrerollRingBuffer(max_frames=0)
    buf.push(b"x")
    assert buf.drain() == b""


def test_clear_discards_stale_frames():
    """Must clear after an utterance ends, otherwise the tail of the previous sentence gets spliced onto the start of the next."""
    buf = PrerollRingBuffer(max_frames=3)
    buf.push(b"old1")
    buf.push(b"old2")
    buf.clear()
    assert len(buf) == 0
    buf.push(b"new")
    assert buf.drain() == b"new"
