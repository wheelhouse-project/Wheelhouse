"""Test the proposed 'simplify' instruction against Gemma 4 E4B.

The instruction under test is the exact string from the proposed pattern:

    "Rewrite this text in plain language. Use shorter sentences and simpler
     words. Keep every fact. Return only the rewritten text."

Sent as the system prompt, with the captured selection as the user message,
which is the shape rewrite_text_ai would use.

Failure modes checked, each seen in the earlier fix-command measurements:
  - added preamble or commentary
  - dropped facts (names, numbers, dates)
  - destroyed line structure (the selection may be a list)
  - already-simple text damaged or padded
  - markdown escape characters leaking into plain text
  - non-ASCII corruption
"""
import json
import re
import subprocess
import sys
import time
import urllib.request

BIN = r"D:\llm-eval\bin\vulkan\llama-server.exe"
MODEL = r"D:\llm-eval\models\gemma-4-E4B_q4_0-it.gguf"
PORT = 8901
BASE = f"http://127.0.0.1:{PORT}"
TRIALS = 5

INSTRUCTION = (
    "Rewrite this text in plain language. Use shorter sentences and simpler "
    "words. Keep every fact. Return only the rewritten text."
)

CASES = [
    {
        "id": "dense-prose",
        "text": (
            "Notwithstanding the aforementioned provisions, the policyholder "
            "shall be indemnified for any loss arising from the peril "
            "enumerated in Schedule B, provided that written notification is "
            "furnished to the insurer within thirty days of the occurrence."
        ),
        "must_keep": ["Schedule B"],
        "keep_any": [["thirty", "30"]],
    },
    {
        "id": "technical",
        "text": (
            "The WebSocket connection between the speech recognition process "
            "and the logic process terminates approximately once every three "
            "hours, which is most likely attributable to an absent keepalive "
            "frame on the intervening proxy."
        ),
        "must_keep": [],
        "keep_any": [["three", "3"]],
    },
    {
        "id": "already-simple",
        "text": "The report is done. I sent it to Karen on Friday.",
        "must_keep": ["Karen", "Friday"],
        "keep_any": [],
    },
    {
        "id": "one-short-sentence",
        "text": "Please remit payment.",
        "must_keep": [],
        "keep_any": [],
    },
    {
        "id": "multi-line-list",
        "text": (
            "Steps to reproduce:\n"
            "1. Open the settings window.\n"
            "2. Change the microphone to the USB device.\n"
            "3. Click Save and observe that the change is discarded."
        ),
        "must_keep": ["USB"],
        "keep_any": [],
    },
    {
        "id": "names-and-numbers",
        "text": (
            "Dr. Smith confirmed that the invoice from Acme Corporation "
            "totaled $123.50 and indicated he would telephone me on Tuesday, "
            "March 15th, at 9:45 AM."
        ),
        "must_keep": ["Smith", "Acme", "123.50", "Tuesday"],
        "keep_any": [["9:45", "9.45"]],
    },
]

PREAMBLE = re.compile(
    r"^\s*(here(?:'s| is)|sure|okay|ok|certainly|of course|rewritten|"
    r"simplified|plain language version|i've|i have)\b",
    re.IGNORECASE,
)
TRAILING_NOTE = re.compile(
    r"\n\s*(note|i (?:kept|changed|simplified|replaced)|let me know|"
    r"changes made|explanation)\b",
    re.IGNORECASE,
)
MD_ESCAPE = re.compile(r"\\[.\-#*_$()\[\]]")


def wait_healthy(proc, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early, code {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                if json.loads(r.read()).get("status") == "ok":
                    return time.time() - start
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("server never became healthy")


def ask(system, user):
    body = json.dumps({
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"},
    )
    t = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        payload = json.loads(r.read())
    dt = time.time() - t
    choice = payload["choices"][0]
    return choice["message"].get("content") or "", choice.get("finish_reason"), dt


def check(case, out):
    problems = []
    if not out.strip():
        return ["EMPTY RESPONSE"]
    if PREAMBLE.search(out):
        problems.append("preamble")
    if TRAILING_NOTE.search(out):
        problems.append("trailing commentary")
    if MD_ESCAPE.search(out):
        problems.append("markdown escape")
    try:
        out.encode("ascii")
    except UnicodeEncodeError:
        problems.append("non-ASCII")
    for token in case["must_keep"]:
        if token.lower() not in out.lower():
            problems.append(f"dropped {token!r}")
    for group in case["keep_any"]:
        if not any(t.lower() in out.lower() for t in group):
            problems.append(f"dropped all of {group}")
    if "\n" in case["text"] and "\n" not in out:
        problems.append("line structure flattened")
    if len(out) > len(case["text"]) * 1.4:
        problems.append(f"longer than input ({len(case['text'])} -> {len(out)})")
    return problems


def main():
    cmd = [BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-c", "8192", "-ngl", "99", "--no-webui",
           "--reasoning", "off", "--reasoning-budget", "0"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        load = wait_healthy(proc)
        print(f"loaded in {load:.1f}s\n")
        totals = {}
        for case in CASES:
            print(f"=== {case['id']} ===")
            print(f"IN  ({len(case['text'])} chars): {case['text']}\n")
            fails = 0
            for i in range(TRIALS):
                out, reason, dt = ask(INSTRUCTION, case["text"])
                problems = check(case, out)
                if problems:
                    fails += 1
                mark = "FAIL" if problems else "ok  "
                print(f"[{mark}] trial {i+1} ({dt:.2f}s, finish={reason})"
                      f"{'  <- ' + ', '.join(problems) if problems else ''}")
                if i == 0 or problems:
                    print(f"    OUT: {out.strip()[:600]}")
            totals[case["id"]] = (fails, TRIALS)
            print()
        print("=== summary ===")
        for cid, (f, n) in totals.items():
            print(f"{cid:22s} {f}/{n} failed")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
