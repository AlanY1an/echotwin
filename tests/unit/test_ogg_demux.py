from echotwin.audio.ogg_demux import OggDemuxer


def test_empty_feed_no_output():
    demux = OggDemuxer()
    demux.feed(b"")
    assert list(demux.packets()) == []


def test_garbage_input_skipped():
    demux = OggDemuxer()
    demux.feed(b"not_an_ogg_stream_at_all")
    demux.feed(b"OggS\x00")
    assert list(demux.packets()) == []


def test_api_present():
    demux = OggDemuxer()
    assert hasattr(demux, "feed")
    assert hasattr(demux, "packets")
    assert hasattr(demux, "flush")
