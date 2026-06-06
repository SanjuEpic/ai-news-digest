"""
Calibrate the dedup similarity threshold for the AI Digest.

Embeds controlled headline pairs at 512 MRL dims (same as production) and prints
cosine similarities so we can pick a threshold that:
  - catches true paraphrase duplicates (should score HIGH)
  - keeps genuinely different news distinct (should score LOW)
  - does NOT over-merge "hard negatives" (same topic, different specifics)
"""
import os
import json
import math
import urllib.request

EMBED_DIMS = 512
MODEL = "text-embedding-3-small"


def get_openai_key() -> str:
    k = os.environ.get("OPENAI_API_KEY")
    if k:
        return k
    # repo root is three levels up: tools/ -> ai-digest/ -> jobs/ -> scheduled_crons/
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("OPENAI_API_KEY not found")


def embed(texts, key):
    payload = json.dumps({"model": MODEL, "input": texts, "dimensions": EMBED_DIMS}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    items = sorted(data["data"], key=lambda d: d["index"])
    return [it["embedding"] for it in items]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# (A, B, expected)  expected: "DUP" = should be caught, "DIFF" = should stay distinct
PAIRS = [
    # --- TRUE DUPLICATES (paraphrases of the same news) ---
    ("ChatGPT Hits 1 Billion Monthly Active Users Milestone",
     "ChatGPT reaches 1B monthly active users for the first time", "DUP"),
    ("OpenAI Grants EU Access to GPT-5.5-Cyber for Security",
     "OpenAI opens GPT-5.5-Cyber to European security researchers", "DUP"),
    ("Trump signs AI frontier model review order; 30-day pre-release access path",
     "US President signs executive order requiring 30-day frontier AI model reviews", "DUP"),
    ("Google releases Gemini 3.5 Flash, 4x faster than 3.1 Pro",
     "Gemini 3.5 Flash now generally available, runs 4x faster than Gemini 3.1 Pro", "DUP"),
    ("DeepSeek V4 Pro ties closed frontier at 80.6% SWE-Bench Verified",
     "DeepSeek V4 Pro matches top closed models with 80.6% on SWE-Bench Verified", "DUP"),

    # --- HARD NEGATIVES (same topic / entity, genuinely different news) ---
    ("Google releases Gemini 3.5 Flash",
     "Google confirms Gemini 3.5 Pro coming next month", "DIFF"),
    ("OpenAI grants EU access to GPT-5.5-Cyber for security",
     "OpenAI launches GPT-5.5 for enterprise customers in the US", "DIFF"),
    ("Anthropic releases Claude Opus 4.8, #1 on Artificial Analysis Index",
     "Anthropic previews Claude Mythos for defensive cybersecurity", "DIFF"),
    ("Qwen 3.6 adds support for 200+ languages",
     "Qwen3 Coder Next released for agentic coding", "DIFF"),

    # --- EASY NEGATIVES (clearly unrelated news) ---
    ("ChatGPT Hits 1 Billion Monthly Active Users",
     "NVIDIA Nemotron 3 Nano Omni unifies vision, speech, and language", "DIFF"),
    ("Trump signs AI frontier model review order",
     "Mistral Large 3 leads multilingual benchmarks across 40 languages", "DIFF"),
    ("Mercury 2 diffusion LLM hits 1000+ tokens/second",
     "Microsoft Build unveils MAI Voice 2 with emotional control", "DIFF"),
]


def main():
    key = get_openai_key()
    texts = []
    for a, b, _ in PAIRS:
        texts.extend([a, b])
    vecs = embed(texts, key)

    results = []
    for i, (a, b, exp) in enumerate(PAIRS):
        va, vb = vecs[2 * i], vecs[2 * i + 1]
        results.append((cosine(va, vb), exp, a, b))

    print(f"\n=== Cosine similarities @ {EMBED_DIMS} dims ({MODEL}) ===\n")
    dup_scores, diff_scores = [], []
    for score, exp, a, b in sorted(results, reverse=True):
        tag = "DUP " if exp == "DUP" else "DIFF"
        (dup_scores if exp == "DUP" else diff_scores).append(score)
        print(f"  {score:.3f}  [{tag}]  {a[:42]:42s} || {b[:42]}")

    min_dup = min(dup_scores)
    max_diff = max(diff_scores)
    print("\n--- Summary ---")
    print(f"  TRUE DUPLICATES : min={min(dup_scores):.3f}  max={max(dup_scores):.3f}  (want these CAUGHT)")
    print(f"  TRUE DIFFERENT  : min={min(diff_scores):.3f}  max={max(diff_scores):.3f}  (want these KEPT)")
    print(f"\n  Lowest duplicate score : {min_dup:.3f}")
    print(f"  Highest different score: {max_diff:.3f}")

    if min_dup > max_diff:
        mid = (min_dup + max_diff) / 2
        print(f"\n  ✅ CLEAN SEPARATION. Any threshold in ({max_diff:.3f}, {min_dup:.3f}) works.")
        print(f"  👉 Recommended threshold: {mid:.2f} (midpoint, max margin)")
    else:
        print(f"\n  ⚠️ OVERLAP between {max_diff:.3f} and {min_dup:.3f} — no perfect split.")
        print(f"     A threshold of 0.80 would catch dups down to 0.80 but may over-merge")
        print(f"     hard negatives scoring above 0.80. Inspect the rows above.")


if __name__ == "__main__":
    main()
