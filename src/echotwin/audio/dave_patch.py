"""Runtime monkey-patch to add DAVE E2EE decryption to discord-ext-voice-recv.

After Discord enforced DAVE end-to-end encryption on 2026-03-02, the
`discord-ext-voice-recv` library still only does RTP-layer decryption.
The opus payload is then *still* encrypted with the per-user DAVE keys,
so libopus rejects it as "corrupted stream".

This module wraps `PacketDecryptor.decrypt_rtp` on each `AudioReader`
instance so that, after RTP-layer decryption, the result is also passed
through the active `DaveSession.decrypt(user_id, MediaType.audio, ...)`.

Apply once at startup before `voice_client.listen(...)` is called:

    from echotwin.audio.dave_patch import apply_dave_patch
    apply_dave_patch()

Falls through to plain RTP-decrypted bytes when:
- davey not installed
- voice connection has no active DAVE session
- DAVE session is in passthrough mode
- ssrc → user_id mapping unknown
- decrypt raises (e.g. wrong epoch)
"""
from __future__ import annotations

from loguru import logger

try:
    import davey  # noqa: F401
    from discord.ext.voice_recv import reader as _voice_recv_reader

    _HAS_DEPS = True
except Exception as _e:  # pragma: no cover
    logger.warning(f"DAVE patch unavailable: {_e}")
    _HAS_DEPS = False


_PATCHED = False


def apply_dave_patch() -> bool:
    """Install the patch. Idempotent: safe to call multiple times."""
    global _PATCHED
    if _PATCHED:
        return True
    if not _HAS_DEPS:
        return False

    AudioReader = _voice_recv_reader.AudioReader
    original_init = AudioReader.__init__

    def patched_init(self, sink, voice_client, *args, **kwargs):
        original_init(self, sink, voice_client, *args, **kwargs)
        _wrap_decryptor(self, voice_client)

    AudioReader.__init__ = patched_init  # type: ignore[assignment]
    _PATCHED = True
    logger.info("DAVE decryption patch applied to discord-ext-voice-recv AudioReader")
    return True


# RTP payload type for opus audio on Discord voice. Video/RTX packets carry
# other payload types and must NOT enter the audio decrypt path (PR #54).
_OPUS_PAYLOAD_TYPE = 120


def _wrap_decryptor(reader_instance, voice_client):
    """Wrap reader_instance.decryptor.decrypt_rtp to also strip DAVE."""
    original_decrypt_rtp = reader_instance.decryptor.decrypt_rtp

    # Counters for diagnostics. Expected failures (Unencrypted... during the
    # DAVE handshake) are counted separately from real failures so a genuine
    # mid-session failure mode (e.g. epoch desync) is ALWAYS visible in logs.
    state = {
        "dave_ok": 0,
        "dave_passthrough": 0,
        "dave_fail_expected": 0,
        "dave_fail_unexpected": 0,
        "no_user": 0,
        "non_opus": 0,
        "passthrough_set_for": None,  # id() of the session we configured
    }
    # Exposed for the watchdog stats log
    reader_instance._dave_patch_state = state

    def dave_aware_decrypt_rtp(packet):
        rtp_pt = original_decrypt_rtp(packet)
        try:
            conn = voice_client._connection
            dave_session = getattr(conn, "dave_session", None)
            if dave_session is None or not getattr(dave_session, "ready", False):
                state["dave_passthrough"] += 1
                if state["dave_passthrough"] in (1, 100, 1000):
                    logger.debug(
                        f"[dave_patch] dave_session not ready "
                        f"(count={state['dave_passthrough']}), passing RTP plaintext through"
                    )
                return rtp_pt

            # Reduce transition-window false rejections: davey starts with
            # passthrough disabled, so unencrypted frames around handshake /
            # epoch transitions raise instead of passing. Configure once per
            # session object (reinit creates a new one → configure again).
            if state["passthrough_set_for"] != id(dave_session):
                try:
                    dave_session.set_passthrough_mode(True, 10)
                    state["passthrough_set_for"] = id(dave_session)
                    logger.info("[dave_patch] passthrough mode enabled (expiry=10s)")
                except Exception as e:
                    logger.debug(f"[dave_patch] set_passthrough_mode failed: {e}")

            # Only opus audio goes through DAVE audio decryption
            if getattr(packet, "payload", _OPUS_PAYLOAD_TYPE) != _OPUS_PAYLOAD_TYPE:
                state["non_opus"] += 1
                if state["non_opus"] in (1, 100):
                    logger.debug(
                        f"[dave_patch] non-opus payload type "
                        f"{getattr(packet, 'payload', '?')} skipped "
                        f"(count={state['non_opus']})"
                    )
                return rtp_pt

            user_id = voice_client._ssrc_to_id.get(packet.ssrc)
            if not user_id:
                state["no_user"] += 1
                if state["no_user"] in (1, 100):
                    logger.debug(
                        f"[dave_patch] unknown ssrc={packet.ssrc} "
                        f"(count={state['no_user']}), passing RTP plaintext through"
                    )
                return rtp_pt

            try:
                opus = dave_session.decrypt(user_id, davey.MediaType.audio, rtp_pt)
                state["dave_ok"] += 1
                if state["dave_ok"] in (1, 100, 1000):
                    logger.info(
                        f"[dave_patch] DAVE decrypted OK count={state['dave_ok']} "
                        f"(user_id={user_id}, ssrc={packet.ssrc})"
                    )
                return opus
            except Exception as e:
                # 'UnencryptedWhenPassthroughDisabled' is expected during the
                # DAVE handshake — packet wasn't DAVE-encrypted to begin with.
                msg = str(e)
                if "Unencrypted" in msg:
                    state["dave_fail_expected"] += 1
                else:
                    state["dave_fail_unexpected"] += 1
                    n = state["dave_fail_unexpected"]
                    if n in (1, 10) or n % 100 == 0:
                        logger.warning(
                            f"[dave_patch] DAVE decrypt FAILED "
                            f"(unexpected_count={n}, user_id={user_id}): {e}; "
                            f"using RTP plaintext (opus decode will fail)"
                        )
                return rtp_pt
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dave_patch] outer error: {e}")
            return rtp_pt

    reader_instance.decryptor.decrypt_rtp = dave_aware_decrypt_rtp  # type: ignore[assignment]
