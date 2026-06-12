"""hard_verdict — first-layer instant judgment: hard rules produce a result, gray zone returns None (handed to the LLM arbiter)."""
from echotwin.pipeline.organic import OrganicContext, Verdict, hard_verdict

WAKE = ["Hinata", "宝宝"]


def _ctx(**kw):
    return OrganicContext(wake_words=WAKE, others_present=["小明", "阿杰"], **kw)


def test_wake_word_at_edge_instant_accept():
    v, score, sigs = hard_verdict("宝宝今天天气怎么样", _ctx())
    assert v == Verdict.ACCEPT and "wake_word" in sigs


def test_wake_word_mid_sentence_is_gray():
    assert hard_verdict("因为我的设定他是宝宝军团的", _ctx()) is None


def test_solo_instant_accept():
    v, _, sigs = hard_verdict("今天好热啊", _ctx(solo=True))
    assert v == Verdict.ACCEPT and "solo" in sigs


def test_fragment_instant_reject():
    v, _, sigs = hard_verdict("你帮你", _ctx(in_window=True))
    assert v == Verdict.REJECT and "fragment" in sigs


def test_ack_is_gray():
    """She asks "want me to continue?" and the other person answers "好的" — whether to take it depends on context; a word list can't decide."""
    assert hard_verdict("好的", _ctx(in_window=True)) is None
    assert hard_verdict("哈哈哈", _ctx()) is None


def test_vocative_other_is_gray():
    """"小明你怎么看她刚说的" is a context question — hand it to the LLM."""
    assert hard_verdict("小明你昨天怎么没上线", _ctx(in_window=True)) is None


def test_out_of_window_chatter_is_gray():
    """The reflex layer no longer pre-judges "relevance" — out-of-window chatter is also sent for review; judgment belongs to the LLM."""
    assert hard_verdict("哎法国配合也还可以的我觉得", _ctx()) is None


def test_out_of_window_imperative_is_gray():
    """"帮我放首歌" is an out-of-window imperative; the old trigger regex would miss it — now it must go to the gray zone."""
    assert hard_verdict("帮我放首歌", _ctx()) is None


def test_in_window_is_gray():
    assert hard_verdict("你能不能在这版本的结束啊", _ctx(in_window=True)) is None


def test_clarify_pending_is_gray():
    """After a clarify question even "嗯" carries meaning — hand everything to the LLM."""
    assert hard_verdict("嗯", _ctx(clarify_pending=True)) is None
