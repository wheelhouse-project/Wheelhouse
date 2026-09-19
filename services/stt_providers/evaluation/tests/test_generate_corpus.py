"""Guard tests for the corpus generator's category-to-directory mapping.

These exist because of a concrete failure on 2026-08-24. vocabulary.py gained
nine va_* categories for the Voice Access parity work; generate_corpus.py
carried a hand-maintained eight-entry dict in category_dir and a second
hand-maintained list of the same eight names in ensure_output_dirs. Nothing
tied either to the vocabulary, so the run started, synthesized 303 of 853
files, and then died with KeyError: 'va_punctuation_symbols' the first time an
utterance from a new category reached category_dir.

The test below fails against that old code and passes against the current
code, which derives both from the vocabulary itself.
"""

import sys
from pathlib import Path

import pytest

EVALUATION_DIR = Path(__file__).resolve().parent.parent
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

generate_corpus = pytest.importorskip(
    "generate_corpus",
    reason="needs edge_tts and pydub, which only the shared provider venv installs",
)
from vocabulary import build_vocabulary  # noqa: E402


def test_every_vocabulary_category_maps_to_a_directory():
    """category_dir accepts every category the vocabulary actually produces.

    This is the assertion that would have stopped the 2026-08-24 run before it
    wasted 303 files of synthesis. It is deliberately driven by the vocabulary
    rather than by a list written here, so a category added later is covered
    without editing this test.
    """
    categories = {u.category for u in build_vocabulary()}
    assert categories, "build_vocabulary produced no utterances"

    for category in sorted(categories):
        subdir = generate_corpus.category_dir(category)
        assert subdir, f"category {category!r} mapped to an empty directory name"


def test_directory_names_are_safe_path_segments():
    """No category name can escape the corpus directory or break a path.

    category_dir now returns the category unchanged, so a category containing a
    separator or a parent reference would write outside CORPUS_DIR.
    """
    for category in sorted({u.category for u in build_vocabulary()}):
        subdir = generate_corpus.category_dir(category)
        assert "/" not in subdir and "\\" not in subdir, (
            f"category {category!r} contains a path separator"
        )
        assert subdir not in (".", ".."), f"category {category!r} is a path traversal"
        assert subdir == subdir.strip(), f"category {category!r} has edge whitespace"


def test_ensure_output_dirs_creates_one_directory_per_category(tmp_path, monkeypatch):
    """ensure_output_dirs covers the whole vocabulary, not a fixed list.

    The old implementation took no argument and created exactly eight fixed
    directories, so this test fails against it twice over: the call signature
    differs, and the nine va_* directories are absent.
    """
    monkeypatch.setattr(generate_corpus, "CORPUS_DIR", tmp_path)
    vocabulary = build_vocabulary()

    generate_corpus.ensure_output_dirs(vocabulary)

    expected = {u.category for u in vocabulary}
    created = {p.name for p in tmp_path.iterdir() if p.is_dir()}
    assert expected <= created, f"missing directories: {sorted(expected - created)}"
