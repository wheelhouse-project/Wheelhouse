"""Analyze STT log for revision events.

A revision occurs when a stable or final message does NOT start with
the previous stable text for the same utterance. This script parses
the stt_server.log and counts how often revisions happen.
"""
import sys
import io
import re
from collections import defaultdict
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Pattern to match STABLE and FINAL lines
# [2025-12-10 08:31:50.737] [STABLE] UTT-11: okay
# [2025-12-10 08:31:52.840] [FINAL] UTT-11: okay now it's running with the new version
LINE_PATTERN = re.compile(r'\[(STABLE|FINAL)\] UTT-(\d+): (.*)$')


def analyze_log(log_path: Path) -> dict:
    """Parse log file and detect revisions.

    Important: We track utterances by sequence, not just ID, because
    utterance IDs reset when the stream restarts. We detect stream restarts
    by looking for ID decreases or FINAL followed by lower ID.
    """

    # Track state per utterance - use (session, utt_id) as key
    current_session = 0
    last_utt_id = 0
    utterance_last_stable: dict[tuple[int, int], str] = {}  # (session, utt_id) -> last stable text

    # Counters
    stats = {
        'total_sessions': 1,
        'total_utterances': 0,
        'utterances_with_stables': 0,
        'total_stables': 0,
        'total_finals': 0,
        'stable_revisions': 0,  # Stable doesn't start with previous stable
        'final_revisions': 0,   # Final doesn't start with last stable
        'revision_examples': [],  # Store examples for review
        'seen_utterances': set(),  # (session, utt_id) pairs
    }

    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        for line_num, line in enumerate(f, 1):
            match = LINE_PATTERN.search(line)
            if not match:
                continue

            msg_type = match.group(1)  # STABLE or FINAL
            utt_id = int(match.group(2))
            text = match.group(3).strip()

            # Redacted logs (the release default, LOG_TRANSCRIPTS=false)
            # carry '<redacted: N chars, M words>' placeholders whose
            # counts differ between successive stables, so every prefix
            # comparison fails and the revision rate reads near-100%
            # garbage. Refuse to analyze rather than mislead (wh-797.17.4).
            if "<redacted:" in text:
                print(f"[!] {log_path} was recorded with transcript logging off "
                      f"(the release default): line {line_num} carries a redaction "
                      f"placeholder instead of text.")
                print("[!] Revision analysis needs real transcripts. Set "
                      "LOG_TRANSCRIPTS = true in config.toml, restart WheelHouse, "
                      "reproduce, then re-run this script on the new log.")
                sys.exit(2)

            # Detect session restart: ID decreased significantly or we see an ID we already finalized
            if utt_id < last_utt_id - 5:  # Allow small out-of-order but detect restarts
                current_session += 1
                stats['total_sessions'] += 1
                utterance_last_stable.clear()  # Clear state for new session

            last_utt_id = utt_id
            key = (current_session, utt_id)

            if key not in stats['seen_utterances']:
                stats['seen_utterances'].add(key)
                stats['total_utterances'] += 1

            if msg_type == 'STABLE':
                stats['total_stables'] += 1

                # First stable for this utterance?
                if key not in utterance_last_stable:
                    stats['utterances_with_stables'] += 1

                # Check for revision against previous stable
                if key in utterance_last_stable:
                    prev_stable = utterance_last_stable[key]
                    if not text.startswith(prev_stable):
                        stats['stable_revisions'] += 1
                        if len(stats['revision_examples']) < 30:
                            stats['revision_examples'].append({
                                'type': 'stable_revision',
                                'utt_id': utt_id,
                                'session': current_session,
                                'previous': prev_stable,
                                'current': text,
                                'line': line_num,
                            })

                utterance_last_stable[key] = text

            elif msg_type == 'FINAL':
                stats['total_finals'] += 1

                # Check for revision against last stable
                if key in utterance_last_stable:
                    last_stable = utterance_last_stable[key]
                    if not text.startswith(last_stable):
                        stats['final_revisions'] += 1
                        if len(stats['revision_examples']) < 30:
                            stats['revision_examples'].append({
                                'type': 'final_revision',
                                'utt_id': utt_id,
                                'session': current_session,
                                'last_stable': last_stable,
                                'final': text,
                                'line': line_num,
                            })

                # Clear state for this utterance (it's done)
                if key in utterance_last_stable:
                    del utterance_last_stable[key]

    return stats


def print_report(stats: dict):
    """Print analysis report."""
    total_utts = stats['total_utterances']
    utts_with_stables = stats['utterances_with_stables']

    print("=" * 60)
    print("STT Revision Analysis Report")
    print("=" * 60)
    print()
    print(f"Total sessions (restarts):  {stats['total_sessions']:,}")
    print(f"Total utterances:           {total_utts:,}")
    print(f"Utterances with stables:    {utts_with_stables:,}")
    print(f"Total stable messages:      {stats['total_stables']:,}")
    print(f"Total final messages:       {stats['total_finals']:,}")
    print()
    print("-" * 60)
    print("REVISIONS DETECTED")
    print("-" * 60)
    print(f"Stable-to-stable revisions: {stats['stable_revisions']:,}")
    print(f"Stable-to-final revisions:  {stats['final_revisions']:,}")
    print(f"Total revisions:            {stats['stable_revisions'] + stats['final_revisions']:,}")
    print()

    if utts_with_stables > 0:
        revision_rate = (stats['stable_revisions'] + stats['final_revisions']) / utts_with_stables * 100
        print(f"Revision rate:              {revision_rate:.2f}% of utterances with stables")

    if stats['total_stables'] > 0:
        stable_rev_rate = stats['stable_revisions'] / stats['total_stables'] * 100
        print(f"Stable revision rate:       {stable_rev_rate:.4f}% of stable messages")

    print()
    print("-" * 60)
    print("REVISION EXAMPLES")
    print("-" * 60)

    for i, ex in enumerate(stats['revision_examples'], 1):
        print(f"\n[{i}] {ex['type']} (UTT-{ex['utt_id']}, line {ex['line']})")
        if ex['type'] == 'stable_revision':
            print(f"    Previous stable: '{ex['previous']}'")
            print(f"    New stable:      '{ex['current']}'")
        else:
            print(f"    Last stable:     '{ex['last_stable']}'")
            print(f"    Final:           '{ex['final']}'")


def main():
    if len(sys.argv) > 1:
        log_path = Path(sys.argv[1])
    else:
        log_path = (Path(__file__).parent.parent / 'services' / 'stt_providers'
                    / 'google_stt_server' / 'stt_server.log')

    if not log_path.exists():
        print(f"Error: Log file not found: {log_path}")
        sys.exit(1)

    print(f"Analyzing: {log_path}")
    print(f"File size: {log_path.stat().st_size / 1024 / 1024:.1f} MB")
    print()

    stats = analyze_log(log_path)
    print_report(stats)


if __name__ == '__main__':
    main()
