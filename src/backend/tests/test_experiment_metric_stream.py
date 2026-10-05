"""Metrics are framed by protocol lines, independently of transport reads."""

import pytest

from app.services.experiment_metric_stream import MetricLineFramer


def test_metric_line_split_across_reads_is_emitted_once():
    framer = MetricLineFramer()
    assert framer.feed("stdout", 'POLARIS_METRIC {"name":"acc",') == ""
    line = 'POLARIS_METRIC {"name":"acc","value":0.8}\n'
    assert framer.feed("stdout", '"value":0.8}\nnext') == line
    assert framer.finish() == "next"
    assert framer.finish() == ""


def test_streams_do_not_splice_metric_fragments():
    framer = MetricLineFramer()
    assert framer.feed("stdout", "POLARIS_METRIC ") == ""
    assert framer.feed("stderr", '{"value":9}\n') == '{"value":9}\n'
    assert framer.feed("stdout", '{"value":1}\n') == 'POLARIS_METRIC {"value":1}\n'


def test_terminal_tails_keep_stream_boundary_and_flush_once():
    framer = MetricLineFramer()
    framer.feed("stdout", "first")
    framer.feed("stderr", "second")
    assert framer.finish("stderr") == "second"
    assert framer.finish() == "first"
    assert framer.finish() == ""


def test_unterminated_oversized_line_is_discarded_until_newline():
    framer = MetricLineFramer(max_pending_chars=8)
    assert framer.feed("stdout", "0123456789") == ""
    assert framer.feed("stdout", "more") == ""
    assert framer.feed("stdout", "bad-line-end\ngood\ntail") == "good\n"
    assert framer.finish() == "tail"


def test_all_terminal_stream_tails_are_separated():
    framer = MetricLineFramer()
    framer.feed("stdout", "first")
    framer.feed("stderr", "second")
    assert framer.finish() == "first\nsecond"


def test_pending_limit_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        MetricLineFramer(max_pending_chars=0)
