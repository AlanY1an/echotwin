"""dave_patch's decryption wrapper — payload filtering / passthrough / visibility of real failures.

Based on dev-docs/2026-06-11-voice-detection/2026-06-11-dave-voice-receive-research.md §6:
- Only opus audio packets (RTP payload type 120) go through DAVE decrypt (PR #54 practice);
- Once the DAVE session is ready, enable set_passthrough_mode(True, 10) to reduce
  false drops during the transition period;
- Expected failures (Unencrypted...) and real failures are counted separately, and real
  failures must always be logged (old logic: log slots 1 and 50 were almost always consumed
  by expected startup failures → real failures were never logged).
"""
from types import SimpleNamespace

from loguru import logger

from echotwin.audio.dave_patch import _wrap_decryptor


class FakeDaveSession:
    def __init__(self, decrypt_result=b"opus-decrypted", raise_msg=None):
        self.ready = True
        self.decrypt_calls = []
        self.passthrough_calls = []
        self._decrypt_result = decrypt_result
        self._raise_msg = raise_msg

    def decrypt(self, user_id, media_type, data):
        self.decrypt_calls.append((user_id, data))
        if self._raise_msg:
            raise RuntimeError(self._raise_msg)
        return self._decrypt_result

    def set_passthrough_mode(self, passthrough_mode, transition_expiry=None):
        self.passthrough_calls.append((passthrough_mode, transition_expiry))


def _make_wrapped(dave_session):
    reader = SimpleNamespace(
        decryptor=SimpleNamespace(decrypt_rtp=lambda pkt: b"rtp-plain")
    )
    vc = SimpleNamespace(
        _connection=SimpleNamespace(dave_session=dave_session),
        _ssrc_to_id={123: 42},
    )
    _wrap_decryptor(reader, vc)
    return reader.decryptor.decrypt_rtp, dave_session


def _packet(ssrc=123, payload=120):
    return SimpleNamespace(ssrc=ssrc, payload=payload)


def test_opus_packet_is_dave_decrypted():
    decrypt_rtp, sess = _make_wrapped(FakeDaveSession())
    out = decrypt_rtp(_packet(payload=120))
    assert out == b"opus-decrypted"
    assert len(sess.decrypt_calls) == 1


def test_non_opus_payload_skips_dave_decrypt():
    """Non-audio packets (video/RTX etc.) must not enter the audio decrypt path (the fix in PR #54)."""
    decrypt_rtp, sess = _make_wrapped(FakeDaveSession())
    out = decrypt_rtp(_packet(payload=101))
    assert out == b"rtp-plain"
    assert sess.decrypt_calls == [], "非 opus 包不应调用 DAVE decrypt"


def test_passthrough_mode_enabled_once_when_ready():
    decrypt_rtp, sess = _make_wrapped(FakeDaveSession())
    decrypt_rtp(_packet())
    decrypt_rtp(_packet())
    assert sess.passthrough_calls == [(True, 10)], (
        "session 就绪后应恰好启用一次 passthrough(True, 10)"
    )


def test_unexpected_failure_is_logged_even_after_expected_ones():
    """Expected startup failures must not consume the log quota for real failures."""
    decrypt_rtp, sess = _make_wrapped(
        FakeDaveSession(raise_msg="Unencrypted frame when passthrough disabled")
    )
    decrypt_rtp(_packet())
    decrypt_rtp(_packet())  # two expected failures

    sess._raise_msg = "MLS epoch mismatch"  # subsequent failures are real ones
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        decrypt_rtp(_packet())
    finally:
        logger.remove(handler_id)

    assert any("epoch mismatch" in m for m in messages), (
        f"第一次真实失败必须打日志,实际: {messages}"
    )
