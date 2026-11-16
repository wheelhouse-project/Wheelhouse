"""Score the answers in a blind Parakeet listening check.

The check file holds one "## Clip NN" section per clip, each with a
"RESULT:" line. David writes A, B, both, or neither after "RESULT:", and may
add the correct words after that first word. The key file (key.json) maps each
clip number to its category ("difference" or "empty") and to the model behind
Text A and Text B.

For each category, the script prints for each model the count of clips judged:
  correct  the answer picked this model's text alone
  wrong    the answer picked the other model's text alone
  both     the answer said both texts match
  neither  the answer said neither text matches

Example (run from this directory):

  python score_listening_check.py --check-file <repo>/PARAKEET-LISTEN-CHECK-2026-11-09.md \
      --key <work-dir>/key.json

This is test tooling (wh-parakeet-listening-check). It uses only the standard
library.
"""

import argparse
import json
import re
import sys
from pathlib import Path

UNREADABLE = "unreadable"
CATEGORIES = ("difference", "empty")
_CATEGORY_TITLES = {"difference": "Difference clips", "empty": "Empty-output clips"}
_CLIP_HEADING = re.compile(r"^##\s+Clip\s+(\d+)\s*$")
_ANY_HEADING = re.compile(r"^#{1,6}\s")
_RESULT = re.compile(r"^RESULT:(.*)$")
_ANSWER = re.compile(r"^(both|neither|a|b)\b", re.IGNORECASE)
_COLUMNS = ("correct", "wrong", "both", "neither")


def _read_answer(rest: str) -> str | None:
    rest = rest.strip()
    if not rest:
        return None
    m = _ANSWER.match(rest)
    return m.group(1).lower() if m else UNREADABLE


def parse_answers(text: str) -> dict[str, str | None]:
    """Map each clip number to "a", "b", "both", "neither", None (blank), or UNREADABLE.

    Only RESULT lines inside a "## Clip NN" section count. Any other heading
    ends the section.
    """
    answers: dict[str, str | None] = {}
    current: str | None = None
    seen_result = False
    for line in text.splitlines():
        heading = _CLIP_HEADING.match(line)
        if heading:
            if current is not None and not seen_result:
                raise ValueError(f"clip {current} has no RESULT line")
            num = heading.group(1)
            if num in answers:
                raise ValueError(f"clip {num} appears twice in the check file")
            answers[num] = None
            current = num
            seen_result = False
            continue
        if _ANY_HEADING.match(line):
            if current is not None and not seen_result:
                raise ValueError(f"clip {current} has no RESULT line")
            current = None
            continue
        result = _RESULT.match(line)
        if result and current is not None:
            if seen_result:
                raise ValueError(f"clip {current} has more than one RESULT line")
            seen_result = True
            answers[current] = _read_answer(result.group(1))
    if current is not None and not seen_result:
        raise ValueError(f"clip {current} has no RESULT line")
    return answers


def score(answers: dict[str, str | None], key: dict) -> dict:
    """Count, per category and per model, the clips judged correct, wrong, both, neither."""
    clips = key["clips"]
    missing = sorted(set(clips) - set(answers))
    extra = sorted(set(answers) - set(clips))
    if missing:
        raise ValueError(f"clips in the key but not in the check file: {', '.join(missing)}")
    if extra:
        raise ValueError(f"clips in the check file but not in the key: {', '.join(extra)}")

    models = sorted({c[f] for c in clips.values() for f in ("text_a_model", "text_b_model")}, reverse=True)
    out = {
        cat: {
            "clips": 0,
            "models": {m: dict.fromkeys(_COLUMNS, 0) for m in models},
            "unanswered": [],
            "unreadable": [],
        }
        for cat in CATEGORIES
    }
    for num in sorted(clips):
        clip = clips[num]
        cat = clip["category"]
        if cat not in out:
            raise ValueError(f"clip {num}: unknown category {cat!r}")
        block = out[cat]
        block["clips"] += 1
        answer = answers[num]
        model_a, model_b = clip["text_a_model"], clip["text_b_model"]
        if answer is None:
            block["unanswered"].append(num)
        elif answer == UNREADABLE:
            block["unreadable"].append(num)
        elif answer in ("both", "neither"):
            block["models"][model_a][answer] += 1
            block["models"][model_b][answer] += 1
        else:
            right, other = (model_a, model_b) if answer == "a" else (model_b, model_a)
            block["models"][right]["correct"] += 1
            block["models"][other]["wrong"] += 1
    return out


def format_report(scores: dict) -> str:
    lines = ["Parakeet listening check score", ""]
    for cat in CATEGORIES:
        block = scores[cat]
        lines.append(f"{_CATEGORY_TITLES[cat]}: {block['clips']}")
        width = max([len("model")] + [len(m) for m in block["models"]])
        lines.append("  " + "model".ljust(width) + "".join(f"{c:>9}" for c in _COLUMNS))
        for model, counts in block["models"].items():
            lines.append("  " + model.ljust(width) + "".join(f"{counts[c]:>9}" for c in _COLUMNS))
        lines.append("  Unanswered: " + (", ".join(block["unanswered"]) or "none"))
        lines.append("  Unreadable: " + (", ".join(block["unreadable"]) or "none"))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score the answers in a blind Parakeet listening check.")
    ap.add_argument("--check-file", required=True, help="the filled check file (markdown)")
    ap.add_argument("--key", required=True, help="key.json from the clip selection")
    args = ap.parse_args(argv)
    answers = parse_answers(Path(args.check_file).read_text(encoding="utf-8"))
    key = json.loads(Path(args.key).read_text(encoding="utf-8"))
    print(format_report(score(answers, key)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
