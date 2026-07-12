"""Analyze STT usage CSV for cost breakdown."""
import csv
from collections import defaultdict
from pathlib import Path

# Read the CSV
csv_path = Path(__file__).parent / "stt_usage.csv"
with open(csv_path, 'r') as f:
    reader = csv.DictReader(f)
    rows = list(reader)

# Aggregate by result_type
stats = defaultdict(lambda: {'count': 0, 'billed_seconds': 0, 'total_words': 0})

for row in rows:
    rt = row['result_type']
    stats[rt]['count'] += 1
    stats[rt]['billed_seconds'] += int(row['billed_seconds'])
    # Handle both old format (had_text: true/false) and new format (word_count: int)
    word_count_val = row.get('word_count', row.get('had_text', '0'))
    if word_count_val in ('true', 'false'):
        # Old format - treat as 1 if true, 0 if false
        stats[rt]['total_words'] += 1 if word_count_val == 'true' else 0
    else:
        # New format - actual word count
        stats[rt]['total_words'] += int(word_count_val) if word_count_val else 0

total_billed = sum(s['billed_seconds'] for s in stats.values())
total_count = sum(s['count'] for s in stats.values())
total_words = sum(s['total_words'] for s in stats.values())

print('=' * 75)
print('STT USAGE COST BREAKDOWN')
print('=' * 75)
print(f'Total utterances: {total_count}')
print(f'Total billed seconds: {total_billed} ({total_billed/60:.1f} minutes)')
print(f'Total words transcribed: {total_words}')
print()

header = f"{'Result Type':<20} {'Count':>8} {'Billed(s)':>10} {'Avg(s)':>8} {'% Cost':>8} {'Words':>8}"
print(header)
print('-' * 75)

for rt in sorted(stats.keys(), key=lambda x: stats[x]['billed_seconds'], reverse=True):
    s = stats[rt]
    avg = s['billed_seconds'] / s['count'] if s['count'] > 0 else 0
    pct = (s['billed_seconds'] / total_billed * 100) if total_billed > 0 else 0
    print(f"{rt:<20} {s['count']:>8} {s['billed_seconds']:>10} {avg:>8.1f} {pct:>7.1f}% {s['total_words']:>8}")

# Calculate waste (0 words = noise)
waste_billed = 0
waste_count = 0
waste_by_type = defaultdict(lambda: {'count': 0, 'billed_seconds': 0})

for row in rows:
    rt = row['result_type']
    billed = int(row['billed_seconds'])
    word_count_val = row.get('word_count', row.get('had_text', '0'))
    
    if word_count_val in ('true', 'false'):
        wc = 1 if word_count_val == 'true' else 0
    else:
        wc = int(word_count_val) if word_count_val else 0
        
    if wc == 0:
        waste_count += 1
        waste_billed += billed
        waste_by_type[rt]['count'] += 1
        waste_by_type[rt]['billed_seconds'] += billed

print()
print('=' * 75)
print('WASTE ANALYSIS (0 Words Transcribed)')
print('=' * 75)
print(f'Wasted utterances: {waste_count} ({waste_count/total_count*100:.1f}% of total)' if total_count > 0 else 'Wasted utterances: 0')
print(f'Wasted billed seconds: {waste_billed} ({waste_billed/60:.1f} minutes)')
print(f'Waste as % of total cost: {waste_billed/total_billed*100:.1f}%' if total_billed > 0 else 'Waste as % of total cost: 0%')
print()
print("Top Sources of Waste:")
waste_header = f"{'Result Type':<20} {'Count':>8} {'Billed(s)':>10} {'% of Waste':>12}"
print(waste_header)
print('-' * 75)
for rt in sorted(waste_by_type.keys(), key=lambda x: waste_by_type[x]['billed_seconds'], reverse=True):
    s = waste_by_type[rt]
    pct = (s['billed_seconds'] / waste_billed * 100) if waste_billed > 0 else 0
    print(f"{rt:<20} {s['count']:>8} {s['billed_seconds']:>10} {pct:>11.1f}%")
print()

# Google pricing: $0.024 per minute for enhanced model
cost_per_minute = 0.024
total_cost = (total_billed / 60) * cost_per_minute
waste_cost = (waste_billed / 60) * cost_per_minute
print(f'Estimated total cost (at $0.024/min): ${total_cost:.2f}')
print(f'Estimated wasted cost: ${waste_cost:.2f}')
