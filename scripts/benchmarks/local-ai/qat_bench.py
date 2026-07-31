"""Benchmark Gemma QAT models under llama-server for the fix / rewrite commands.

For each (model, backend, reasoning) combination this starts llama-server,
waits for it to report healthy, runs every task N times, then stops the server.

Correctness is scored by automatic checks per input, so the reported failure
rate comes from repeated trials rather than a single sample. The checks look
for the specific defects seen in the first comparison round: wrong or dropped
numbers, duplicated amounts, added commentary, non-ASCII corruption, and
markdown escape characters leaking into plain text.
"""
import json
import re
import subprocess
import sys
import time
import urllib.request

BIN_VULKAN = r"D:\llm-eval\bin\vulkan\llama-server.exe"
BIN_CPU = r"D:\llm-eval\bin\cpu\llama-server.exe"
PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"

# Absolute paths. The Gemma 3 entry is the ORDINARY Q4_K_M build, read straight
# out of Ollama's blob store -- Google's Gemma 3 QAT repo is gated and cannot be
# downloaded without an authenticated Hugging Face account. It is included as
# the quality reference point that won the first comparison round.
MODELS = {
    "gemma3-4b-ptq": r"D:\ollama\models\blobs\sha256-aeda25e63ebd698fab8638ffb778e68bed908b960d39d0becc650fa981609d25",
    "gemma4-e2b-qat": r"D:\llm-eval\models\gemma-4-E2B_q4_0-it.gguf",
    "gemma4-e4b-qat": r"D:\llm-eval\models\gemma-4-E4B_q4_0-it.gguf",
    "gemma4-12b-qat": r"D:\llm-eval\models\gemma-4-12b-it-qat-q4_0.gguf",
}

FIX_SYSTEM = """You are a text formatting assistant. Your ONLY job is to fix the formatting of speech-to-text output. You MUST:
- Capitalize proper nouns (names of people, places, companies, products)
- Capitalize the first letter of each sentence
- Format currencies (e.g., "fifty dollars" -> "$50", "twenty five cents" -> "$0.25")
- Format numbers when appropriate (e.g., "one hundred twenty three" -> "123")
- Format common abbreviations (e.g., "doctor" -> "Dr.", "mister" -> "Mr.")
- Fix obvious punctuation where unambiguous

You MUST NOT:
- Rephrase or reword any part of the text
- Add or remove words
- Change the meaning in any way
- Add explanations, notes, or commentary

Return ONLY the corrected text. Nothing else."""

SIMPLIFY_SYSTEM = """You rewrite text to be simpler and easier to read. Keep every fact and the original meaning. Use shorter sentences and plainer words. Do not add information. Do not add explanations, notes, or commentary. Return ONLY the rewritten text."""

CASES = [
    {
        "id": "short/fix",
        "system": FIX_SYSTEM,
        "user": ("i told doctor smith that the invoice from acme corporation "
                 "was one hundred twenty three dollars and fifty cents and he "
                 "said he would call me on tuesday"),
        "must_contain": ["$123.50", "Smith", "Tuesday"],
        # "and fifty cents" catches the duplication seen from E2B, which wrote
        # "$123.50 and fifty cents". A bare "$123.50 and" must NOT be listed
        # here: it also matches the correct sentence "was $123.50 and he said".
        "must_not_contain": ["fifty cents", "one hundred twenty"],
    },
    {
        "id": "medium/fix",
        "system": FIX_SYSTEM,
        "user": ("so i was talking to mister johnson over at northgate medical "
                 "about the appointment on the fifteenth of march and he said "
                 "the copay would be forty five dollars but if we schedule "
                 "before the end of the month it drops to twenty two fifty"),
        "must_contain": ["$45", "$22.50", "Johnson", "Northgate"],
        "must_not_contain": ["$0.225", "$2250"],
    },
    {
        "id": "technical/fix",
        "system": FIX_SYSTEM,
        "user": ("the parakeet t d t model runs on the c p u through sherpa "
                 "onyx and the web socket connection to the logic process "
                 "drops about once every three hours which i think is a keep "
                 "alive problem"),
        "must_contain": ["Sherpa", "CPU"],
        "must_not_contain": ["parrot", "Cortex"],
    },
    {
        "id": "clean/fix",
        "system": FIX_SYSTEM,
        "user": "The report is finished. I sent it to Karen on Friday afternoon.",
        "must_contain": ["Karen", "Friday"],
        "must_not_contain": ["Thursday", "The Report"],
    },
    {
        "id": "medium/simplify",
        "system": SIMPLIFY_SYSTEM,
        "user": ("so i was talking to mister johnson over at northgate medical "
                 "about the appointment on the fifteenth of march and he said "
                 "the copay would be forty five dollars but if we schedule "
                 "before the end of the month it drops to twenty two fifty"),
        "must_contain": ["Johnson"],
        "must_not_contain": ["$0.225"],
    },
]

# Defects that disqualify any output regardless of the case.
COMMENTARY = re.compile(r"\(?Note:|I hope this helps|Let me know|^Here(?:'s| is)",
                        re.IGNORECASE | re.MULTILINE)
ESCAPES = re.compile(r"\\[$#_*]")


def score(case, text):
    """Return a list of defect names; empty list means the output passed."""
    defects = []
    if not text.strip():
        return ["empty"]
    for needle in case["must_contain"]:
        if needle not in text:
            defects.append(f"missing:{needle}")
    for needle in case["must_not_contain"]:
        if needle in text:
            defects.append(f"present:{needle}")
    if not text.isascii():
        bad = sorted({c for c in text if not c.isascii() and c not in "\u2019\u201c\u201d\u2014\u2013"})
        if bad:
            defects.append("non-ascii:" + "".join(bad))
    if COMMENTARY.search(text):
        defects.append("commentary")
    if ESCAPES.search(text):
        defects.append("escape-chars")
    return defects


def wait_healthy(proc, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def chat(system, user, max_tokens=1024):
    body = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        # Deterministic-leaning but still the model's natural behavior; the
        # point is to measure how often it fails, so temperature is left at
        # the model's own default rather than forced to 0.
    }
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "seconds": round(time.time() - t0, 2)}
    dt = round(time.time() - t0, 2)
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    return {
        "seconds": dt,
        "content": (msg.get("content") or "").strip(),
        "reasoning_chars": len(msg.get("reasoning_content") or msg.get("reasoning") or ""),
        "completion_tokens": (data.get("usage") or {}).get("completion_tokens"),
        "finish_reason": choice.get("finish_reason"),
    }


def run_combo(model_key, gguf, backend, reasoning_off, repeats):
    exe = BIN_CPU if backend == "cpu" else BIN_VULKAN
    cmd = [
        exe,
        "-m", gguf,
        "--port", str(PORT),
        "--host", "127.0.0.1",
        "-c", "4096",
        "-ngl", "0" if backend == "cpu" else "99",
        "--no-webui",
    ]
    if reasoning_off:
        cmd += ["--reasoning", "off", "--reasoning-budget", "0"]
    label = f"{model_key}/{backend}/{'nothink' if reasoning_off else 'think'}"
    print(f"### {label}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    load_t0 = time.time()
    if not wait_healthy(proc):
        print("    server failed to start", flush=True)
        try:
            proc.kill()
        except Exception:
            pass
        return {"error": "server failed to start"}
    load_s = round(time.time() - load_t0, 1)
    print(f"    load: {load_s}s", flush=True)

    out = {"load_seconds": load_s, "cases": {}}
    try:
        # One warm-up so the reported timings exclude first-call overhead.
        chat(FIX_SYSTEM, "hello there", max_tokens=32)
        for case in CASES:
            trials = []
            for _ in range(repeats):
                r = chat(case["system"], case["user"])
                if "error" in r:
                    trials.append({"error": r["error"], "defects": ["error"]})
                    continue
                r["defects"] = score(case, r["content"])
                trials.append(r)
            fails = sum(1 for t in trials if t["defects"])
            times = [t["seconds"] for t in trials if "seconds" in t]
            avg = round(sum(times) / len(times), 2) if times else None
            worst = max(times) if times else None
            out["cases"][case["id"]] = {"trials": trials, "fail_rate": f"{fails}/{len(trials)}",
                                        "avg_s": avg, "max_s": worst}
            print(f"    {case['id']}: {fails}/{len(trials)} failed, "
                  f"avg {avg}s max {worst}s", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        time.sleep(2)
    return out


def main():
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    combos = []
    for key, gguf in MODELS.items():
        gemma4 = key.startswith("gemma4")
        # GPU: gemma4 tested both with and without thinking; gemma3 has none.
        combos.append((key, gguf, "gpu", False))
        if gemma4:
            combos.append((key, gguf, "gpu", True))
        # CPU-only: the no-discrete-graphics case, thinking off for gemma4.
        combos.append((key, gguf, "cpu", gemma4))

    results = {}
    for key, gguf, backend, no_think in combos:
        label = f"{key}/{backend}/{'nothink' if no_think else 'think'}"
        results[label] = run_combo(key, gguf, backend, no_think, repeats)
        with open("qat_results.json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
    print("\nwrote qat_results.json", flush=True)


if __name__ == "__main__":
    main()
