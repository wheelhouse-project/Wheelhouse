"""Tests for score_listening_check: score David's answers in the blind
Parakeet listening check (wh-parakeet-listening-check).

Everything runs on small text files; no audio is read.
"""

import json

import pytest

import score_listening_check as slc

V3 = "parakeet-tdt-0.6b-v3"
V2 = "parakeet-tdt-0.6b-v2"

HEAD = """# Parakeet listening check

Write your answer after "RESULT:" under each clip.
For example: `RESULT: neither -- I said open the file`
RESULT: this line is above every clip and must be ignored

---

"""

TAIL = """---

## When you are done

RESULT: this line is below every clip and must be ignored
"""


def _clip(num, result):
    return f"## Clip {num}\n\nPlay: [{num}.wav](x/{num}.wav)\n\nText A: a\n\nText B: b\n\nRESULT: {result}\n\n"


def _check_file(results):
    return HEAD + "".join(_clip(n, r) for n, r in results.items()) + TAIL


def _key(clips):
    """clips: {num: (category, model behind Text A)}."""
    out = {}
    for num, (category, model_a) in clips.items():
        out[num] = {
            "category": category,
            "text_a_model": model_a,
            "text_b_model": V2 if model_a == V3 else V3,
        }
    return {"seed": 1, "clips": out}


# -- parse_answers ------------------------------------------------------


@pytest.mark.parametrize(
    "result, expected",
    [
        ("A", "a"),
        ("a", "a"),
        ("B", "b"),
        ("Both.", "both"),
        ("BOTH", "both"),
        ("neither -- I said open the file", "neither"),
        ("A, the words were scroll down two", "a"),
        ("  b  ", "b"),
        ("", None),
    ],
)
def test_parse_answers_reads_first_word(result, expected):
    answers = slc.parse_answers(_check_file({"01": result}))
    assert answers == {"01": expected}


@pytest.mark.parametrize("result", ["maybe A", "none", "AB", "bothx", "?"])
def test_parse_answers_marks_unreadable(result):
    answers = slc.parse_answers(_check_file({"01": result}))
    assert answers == {"01": slc.UNREADABLE}


def test_parse_answers_ignores_result_lines_outside_clips():
    answers = slc.parse_answers(_check_file({"01": "A", "02": "B"}))
    assert answers == {"01": "a", "02": "b"}


def test_parse_answers_refuses_two_result_lines_in_one_clip():
    text = HEAD + _clip("01", "A").replace("RESULT: A", "RESULT: A\n\nRESULT: B") + TAIL
    with pytest.raises(ValueError, match="01"):
        slc.parse_answers(text)


def test_parse_answers_refuses_a_clip_without_result_line():
    text = HEAD + _clip("01", "A").replace("RESULT: A", "") + TAIL
    with pytest.raises(ValueError, match="01"):
        slc.parse_answers(text)


def test_parse_answers_refuses_a_repeated_clip_number():
    text = HEAD + _clip("01", "A") + _clip("01", "B") + TAIL
    with pytest.raises(ValueError, match="01"):
        slc.parse_answers(text)


# -- score --------------------------------------------------------------


def test_score_maps_answers_to_models_and_splits_categories():
    key = _key({
        "01": ("difference", V3),   # A -> v3 correct, v2 wrong
        "02": ("difference", V2),   # A -> v2 correct, v3 wrong
        "03": ("difference", V3),   # B -> v2 correct, v3 wrong
        "04": ("difference", V2),   # both
        "05": ("difference", V3),   # neither
        "06": ("empty", V2),        # B -> v3 correct, v2 wrong
        "07": ("empty", V3),        # unanswered
        "08": ("empty", V2),        # unreadable
    })
    answers = {"01": "a", "02": "a", "03": "b", "04": "both", "05": "neither",
               "06": "b", "07": None, "08": slc.UNREADABLE}
    s = slc.score(answers, key)

    d = s["difference"]
    assert d["clips"] == 5
    assert d["models"][V3] == {"correct": 1, "wrong": 2, "both": 1, "neither": 1}
    assert d["models"][V2] == {"correct": 2, "wrong": 1, "both": 1, "neither": 1}
    assert d["unanswered"] == []
    assert d["unreadable"] == []

    e = s["empty"]
    assert e["clips"] == 3
    assert e["models"][V3] == {"correct": 1, "wrong": 0, "both": 0, "neither": 0}
    assert e["models"][V2] == {"correct": 0, "wrong": 1, "both": 0, "neither": 0}
    assert e["unanswered"] == ["07"]
    assert e["unreadable"] == ["08"]


def test_score_refuses_a_number_mismatch_between_file_and_key():
    key = _key({"01": ("difference", V3), "02": ("empty", V2)})
    with pytest.raises(ValueError, match="02"):
        slc.score({"01": "a"}, key)
    with pytest.raises(ValueError, match="03"):
        slc.score({"01": "a", "02": "b", "03": "a"}, key)


def test_score_refuses_an_unknown_category():
    key = _key({"01": ("other", V3)})
    with pytest.raises(ValueError, match="other"):
        slc.score({"01": "a"}, key)


# -- main ---------------------------------------------------------------


def test_main_prints_counts_per_model_for_both_categories(tmp_path, capsys):
    key = _key({
        "01": ("difference", V3),
        "02": ("difference", V2),
        "03": ("empty", V3),
    })
    key_path = tmp_path / "key.json"
    key_path.write_text(json.dumps(key), encoding="utf-8")
    check = tmp_path / "check.md"
    check.write_text(_check_file({"01": "A", "02": "A", "03": "neither"}), encoding="utf-8")

    rc = slc.main(["--check-file", str(check), "--key", str(key_path)])
    out = capsys.readouterr().out

    assert rc == 0
    lines = out.splitlines()
    diff_at = next(i for i, ln in enumerate(lines) if ln.startswith("Difference clips"))
    empty_at = next(i for i, ln in enumerate(lines) if ln.startswith("Empty-output clips"))
    assert diff_at < empty_at
    diff_block = lines[diff_at:empty_at]
    empty_block = lines[empty_at:]
    assert "Difference clips: 2" in lines[diff_at]
    assert "Empty-output clips: 1" in lines[empty_at]

    def row(block, model):
        line = next(ln for ln in block if ln.split() and ln.split()[0] == model)
        return [int(x) for x in line.split()[1:]]

    # columns: correct wrong both neither
    assert row(diff_block, V3) == [1, 1, 0, 0]
    assert row(diff_block, V2) == [1, 1, 0, 0]
    assert row(empty_block, V3) == [0, 0, 0, 1]
    assert row(empty_block, V2) == [0, 0, 0, 1]


def test_main_lists_unanswered_and_unreadable_clips(tmp_path, capsys):
    key = _key({"01": ("difference", V3), "02": ("difference", V2)})
    key_path = tmp_path / "key.json"
    key_path.write_text(json.dumps(key), encoding="utf-8")
    check = tmp_path / "check.md"
    check.write_text(_check_file({"01": "", "02": "maybe"}), encoding="utf-8")

    slc.main(["--check-file", str(check), "--key", str(key_path)])
    out = capsys.readouterr().out

    assert "Unanswered: 01" in out
    assert "Unreadable: 02" in out
