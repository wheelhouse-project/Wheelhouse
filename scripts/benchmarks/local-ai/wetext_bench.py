"""Run wetext's English ITN over the same inputs used in the Gemma bench.

Four questions:
  1. Does it produce the written forms the fix command's system prompt asks for?
  2. Does it damage text that is already in written form (the fix command
     captures whatever is selected, which is often not raw speech output)?
  3. How long does it take to construct, and per utterance?
  4. Does it leave clean prose alone?
"""
import time

t0 = time.time()
from wetext import Normalizer
import_s = time.time() - t0

t0 = time.time()
itn = Normalizer(lang="en", operator="itn")
init_s = time.time() - t0

# The exact user inputs from qat_bench.py and length_bench.py.
BENCH = [
    ("short/fix",
     "i told doctor smith that the invoice from acme corporation was one "
     "hundred twenty three dollars and fifty cents and he said he would call "
     "me on tuesday"),
    ("medium/fix",
     "so i was talking to mister johnson over at northgate medical about the "
     "appointment on the fifteenth of march and he said the copay would be "
     "forty five dollars but if we schedule before the end of the month it "
     "drops to twenty two fifty"),
    ("technical/fix",
     "the parakeet t d t model runs on the c p u through sherpa onyx and the "
     "web socket connection to the logic process drops about once every three "
     "hours which i think is a keep alive problem"),
    ("clean/fix",
     "The report is finished. I sent it to Karen on Friday afternoon."),
    ("phrase",
     "remind me to call doctor smith about the invoice on tuesday morning"),
]

# Hallmark categories the retracted NeMo design claimed to cover.
HALLMARKS = [
    ("currency whole", "the total was fifty dollars"),
    ("currency cents", "it costs twenty five cents"),
    ("currency compound", "one hundred twenty three dollars and fifty cents"),
    ("currency bare-fifty", "it drops to twenty two fifty"),
    ("cardinal", "there were three hundred and eighty people"),
    ("ordinal", "the twenty second of june"),
    ("time on hour", "meet me at four o'clock"),
    ("time minutes", "the train leaves at nine forty five a m"),
    ("phone", "call me at seven oh three five five five one two one two"),
    ("date", "on march fifteenth twenty twenty six"),
    ("year", "it happened in nineteen ninety six"),
    ("decimal", "the version is two point five"),
    ("fraction", "add one half cup"),
    ("percent", "battery is at fifty percent"),
]

# Already-written-form text. The fix command captures the selection, which may
# never have passed through speech recognition at all.
WRITTEN_FORM = [
    "The invoice was $123.50 and it is due on 3/15/2026.",
    "Call 703-555-1212 before 9:45 AM.",
    "Version 2.5 shipped in 1996 with 50% fewer defects.",
    "He read chapter twenty two of the novel out loud.",
    "The nineteen eighties were a strange decade.",
    "I need one hundred percent effort from everyone on this team.",
]


def show(label, items):
    print(f"\n=== {label} ===")
    for name, text in items:
        t = time.time()
        out = itn.normalize(text)
        dt = (time.time() - t) * 1000
        changed = "CHANGED" if out != text else "same   "
        print(f"[{changed}] {name} ({dt:.0f} ms)")
        print(f"    in : {text}")
        if out != text:
            print(f"    out: {out}")


print(f"import: {import_s:.2f}s   construct: {init_s:.2f}s")
show("bench inputs", BENCH)
show("hallmarks", HALLMARKS)
show("already written form", [(f"w{i}", t) for i, t in enumerate(WRITTEN_FORM)])

# Timing over repeats, on the longest bench input.
long_text = BENCH[1][1]
times = []
for _ in range(50):
    t = time.time()
    itn.normalize(long_text)
    times.append((time.time() - t) * 1000)
times.sort()
print(f"\nper-utterance over 50 runs on a 42-word input: "
      f"median {times[len(times)//2]:.1f} ms, max {times[-1]:.1f} ms")
