"""Response time by input length, per model and per backend.

Produces the numbers the recommendation table needs: how long the user waits
after saying "fix", for a short phrase, a normal sentence, and a long
paragraph, with and without a graphics card.

Reports tokens per second so the results compare directly against published
figures for other hardware.
"""
import json
import subprocess
import time
import urllib.request

BIN_VULKAN = r"D:\llm-eval\bin\vulkan\llama-server.exe"
BIN_CPU = r"D:\llm-eval\bin\cpu\llama-server.exe"
PORT = 8903
BASE = f"http://127.0.0.1:{PORT}"
REPEATS = 5

MODELS = {
    "E2B-QAT": r"D:\llm-eval\models\gemma-4-E2B_q4_0-it.gguf",
    "E4B-QAT": r"D:\llm-eval\models\gemma-4-E4B_q4_0-it.gguf",
    "12B-QAT": r"D:\llm-eval\models\gemma-4-12b-it-qat-q4_0.gguf",
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

SENTENCE = ("so i was talking to mister johnson over at northgate medical "
            "about the appointment on the fifteenth of march and he said the "
            "copay would be forty five dollars but if we schedule before the "
            "end of the month it drops to twenty two fifty ")

# The long input must be genuinely varied text. An earlier version repeated
# SENTENCE four times; the model recognised the repetition and returned a
# single copy, so the measurement showed the same output length as the short
# case and told us nothing about long-input latency.
PARAGRAPH = (
    "so i was talking to mister johnson over at northgate medical about the "
    "appointment on the fifteenth of march and he said the copay would be "
    "forty five dollars but if we schedule before the end of the month it "
    "drops to twenty two fifty which honestly seems like a good deal to me "
    "anyway i told him i would call back on wednesday afternoon after i talk "
    "to sarah about whether she can drive me there because my car is still at "
    "the shop and they said it would be another three hundred and eighty "
    "dollars to fix the transmission which is more than i wanted to spend this "
    "month but what can you do really and then i need to stop at the pharmacy "
    "on cedar street to pick up the prescription that doctor patel called in "
    "last thursday and also get a birthday card for my nephew who turns "
    "eleven on the twenty second and if there is time i want to swing by the "
    "hardware store because the kitchen faucet has been dripping since "
    "january and i finally found the part number which is r p seven seven "
    "eight five and it costs about fourteen dollars"
)

INPUTS = {
    "phrase (12 words)": "remind me to call doctor smith about the invoice on tuesday morning",
    "sentence (42 words)": SENTENCE.strip(),
    "paragraph (170 words)": PARAGRAPH,
}


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


def chat(user):
    body = {"messages": [{"role": "system", "content": FIX_SYSTEM},
                         {"role": "user", "content": user}],
            "max_tokens": 1024}
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    dt = time.time() - t0
    usage = data.get("usage") or {}
    comp = usage.get("completion_tokens") or 0
    return {"seconds": round(dt, 2), "completion_tokens": comp,
            "prompt_tokens": usage.get("prompt_tokens"),
            "tok_per_s": round(comp / dt, 1) if dt > 0 else None}


def run(name, path, backend):
    exe = BIN_CPU if backend == "cpu" else BIN_VULKAN
    cmd = [exe, "-m", path, "--port", str(PORT), "--host", "127.0.0.1",
           "-c", "8192", "-ngl", "0" if backend == "cpu" else "99",
           "--no-webui", "--reasoning", "off", "--reasoning-budget", "0"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if not wait_healthy(proc):
        print(f"{name}/{backend}: FAILED to start", flush=True)
        try:
            proc.kill()
        except Exception:
            pass
        return {}
    out = {}
    try:
        # First request measured separately: on the Vulkan build this pays a
        # one-time shader compilation cost that must not pollute the averages.
        first = chat("hello there")
        out["first_request_s"] = first.get("seconds")
        print(f"{name}/{backend}: first request {first.get('seconds')}s", flush=True)
        for label, text in INPUTS.items():
            trials = [chat(text) for _ in range(REPEATS)]
            good = [t for t in trials if "seconds" in t]
            if not good:
                continue
            secs = sorted(t["seconds"] for t in good)
            tps = [t["tok_per_s"] for t in good if t["tok_per_s"]]
            out[label] = {
                "median_s": secs[len(secs) // 2],
                "min_s": secs[0],
                "max_s": secs[-1],
                "tok_per_s": round(sum(tps) / len(tps), 1) if tps else None,
                "prompt_tokens": good[0]["prompt_tokens"],
            }
            print(f"    {label}: median {out[label]['median_s']}s "
                  f"(min {out[label]['min_s']}, max {out[label]['max_s']}) "
                  f"{out[label]['tok_per_s']} tok/s", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        time.sleep(2)
    return out


if __name__ == "__main__":
    results = {}
    for name, path in MODELS.items():
        for backend in ("gpu", "cpu"):
            results[f"{name}/{backend}"] = run(name, path, backend)
            with open("length_results.json", "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
    print("\nwrote length_results.json", flush=True)
