"""LatencyJourney — one staged-timing log line per turn, the basis for ongoing optimization."""
import time

from echotwin.utils.latency import LatencyJourney


def test_journey_line_reports_stage_deltas_and_total():
    j = LatencyJourney("endpoint")
    time.sleep(0.01)
    j.mark("asr_done")
    time.sleep(0.02)
    j.mark("first_audio")
    line = j.line()
    assert line.startswith("[latency] ")
    assert "endpoint→asr_done=" in line
    assert "asr_done→first_audio=" in line
    total = int(line.rsplit("total=", 1)[1].rstrip("ms"))
    assert 25 <= total < 500


def test_out_of_order_marks_are_sorted_by_time():
    """drain task and main loop mark() concurrently, so append order can be scrambled — sort by timestamp, no negative deltas."""
    j = LatencyJourney("a")
    time.sleep(0.01)  # must leave a real a→c gap, otherwise b's midpoint lands before a
    j.mark("c")
    # Manually insert b with a timestamp between a and c (simulating out-of-order concurrent append)
    mid = (j._stages[0][1] + j._stages[1][1]) / 2
    j._stages.append(("b", mid))
    line = j.line()
    assert "a→b=" in line and "b→c=" in line
    assert "=-" not in line  # no negative milliseconds
