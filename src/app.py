"""
Weekly AI News Digest Lambda (v2) — runs twice weekly (Mon & Fri)
- Secrets pulled from SSM Parameter Store (SecureString) at cold start
- Claude Haiku + web_search gathers candidate news items (section-tagged)
- Semantic dedup: OpenAI embeddings + cosine similarity (drop items >= threshold)
- Email assembled in Python (no second Claude call) and sent via Gmail SMTP
- Survivors' embeddings stored in DynamoDB with a 14-day TTL (rolling memory)
- On any failure, a classified alert email is sent (credit / rate / key / other)
"""
import os
import json
import time
import math
import array
import hashlib
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3
from anthropic import Anthropic

# ---------------- Config ----------------
REGION = os.environ.get("AWS_REGION", "ap-south-1")
# RECIPIENT_EMAIL may be a single address or a comma-separated list ("a@x.com, b@y.com").
RECIPIENTS = [e.strip() for e in os.environ["RECIPIENT_EMAIL"].split(",") if e.strip()]
RECIPIENT_HEADER = ", ".join(RECIPIENTS)  # for the email "To:" header
SENDER = os.environ["SENDER_EMAIL"]
DDB_TABLE = os.environ["DDB_TABLE"]
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/ai-digest")
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.80"))
MAX_COMPARE = 150  # hard cap on how many stored embeddings we compare against
# MRL (Matryoshka) shortened embedding size. 512 is a native OpenAI MRL training
# breakpoint {512,1024,1536,3072}, so quality is retained while cutting storage ~3x.
EMBED_DIMS = int(os.environ.get("EMBED_DIMS", "512"))

IST = timezone(timedelta(hours=5, minutes=30))
# NOTE: these are (re)computed per-invocation inside handler() so a warm Lambda
# container reused across days never stamps an email with a stale date.
TODAY = datetime.now(IST).strftime("%B %d, %Y").replace(" 0", " ")
TODAY_KEY = datetime.now(IST).strftime("%Y-%m-%d")


def _refresh_today() -> None:
    global TODAY, TODAY_KEY
    now = datetime.now(IST)
    TODAY = now.strftime("%B %d, %Y").replace(" 0", " ")
    TODAY_KEY = now.strftime("%Y-%m-%d")

# ---------------- Secrets from SSM (cold-start cached) ----------------
_ssm = boto3.client("ssm", region_name=REGION)


def _ssm_get(name: str) -> str:
    resp = _ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}", WithDecryption=True)
    return resp["Parameter"]["Value"]


ANTHROPIC_API_KEY = _ssm_get("anthropic-api-key")
GMAIL_APP_PASSWORD = _ssm_get("gmail-app-password")
OPENAI_API_KEY = _ssm_get("openai-api-key")

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(DDB_TABLE)

# ---------------- Section layout ----------------
# canonical key -> (display header, order). TLDR handled specially.
SECTIONS = [
    ("FRONTIER", "🏢 1. Frontier / Closed-Source Models"),
    ("OPENSOURCE", "🔓 2. Open-Source Models"),
    ("MULTIMODAL", "🌐 3. Multimodal & Specialized Models"),
    ("RESEARCH", "🔬 4. Research & Innovations"),
    ("COMPARATIVE", "📊 5. Comparative Snapshot"),
    ("QUICKHITS", "📌 6. Quick Hits"),
]
VALID_SECTIONS = {k for k, _ in SECTIONS} | {"TLDR"}


# ---------------- Embeddings (OpenAI via plain HTTP, no SDK dep) ----------------
def embed(texts: list) -> list:
    """Return a list of float-vectors for the given texts (OpenAI text-embedding-3-small)."""
    if not texts:
        return []
    payload = json.dumps({
        "model": "text-embedding-3-small",
        "input": texts,
        "dimensions": EMBED_DIMS,  # MRL-shortened, returned L2-normalized by OpenAI
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    # Preserve input order
    items = sorted(data["data"], key=lambda d: d["index"])
    return [it["embedding"] for it in items]


def _pack(vec: list) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(b) -> list:
    a = array.array("f")
    a.frombytes(bytes(b))
    return a.tolist()


def _cosine(a: list, b: list) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------- DynamoDB dedup store ----------------
def _key(headline: str) -> str:
    return hashlib.sha256(headline.lower().strip().encode("utf-8")).hexdigest()[:24]


def get_stored_embeddings() -> list:
    """Return list of (headline, vector) for non-expired stored items."""
    out = []
    try:
        resp = table.scan(ProjectionExpression="headline, embedding")
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp and len(items) < MAX_COMPARE:
            resp = table.scan(
                ProjectionExpression="headline, embedding",
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        for it in items[:MAX_COMPARE]:
            emb = it.get("embedding")
            if emb is not None:
                out.append((it.get("headline", ""), _unpack(emb)))
    except Exception as e:
        print(f"[warn] DynamoDB scan failed: {e}")
    return out


def record_items(items: list) -> None:
    """items: list of dicts with headline + vector. Stored with 14-day TTL."""
    expires = int(time.time()) + 14 * 24 * 60 * 60
    with table.batch_writer() as batch:
        for it in items:
            batch.put_item(Item={
                "item_key": _key(it["headline"]),
                "headline": it["headline"][:500],
                "embedding": _pack(it["vector"]),
                "date": TODAY_KEY,
                "expires_at": expires,
            })


import re as _re

# Matches a bracket label that is NOT already inside an anchor's text,
# i.e. a bare "[Label]" not immediately preceded by '>'. We only strip such
# bare labels when the snippet has no real <a href> at all.
_BARE_LABEL = _re.compile(r"\s*\[[^\]\[]{1,30}\]")


def _sanitize_citations(html: str) -> str:
    """Remove dead bracket labels like [Microsoft] that aren't real <a href> links."""
    if not html:
        return html
    has_real_link = _re.search(r'<a\s+href=["\']https?://', html, _re.IGNORECASE)
    if has_real_link:
        # There is at least one real link; only strip bracket labels that are
        # OUTSIDE an anchor (bare). Anchored ones look like >[Label]< inside <a>.
        # Strip any "[Label]" not directly wrapped by an anchor tag.
        def _strip_if_bare(m):
            start = m.start()
            preceding = html[max(0, start - 2):start]
            return m.group(0) if preceding.endswith(">") else ""
        return _BARE_LABEL.sub(_strip_if_bare, html).rstrip()
    # No real link anywhere -> all bracket labels are dead; remove them all.
    return _BARE_LABEL.sub("", html).rstrip()


# ---------------- Phase 1: Claude gathers candidate items ----------------
def gather_candidates() -> list:
    system_prompt = f"""You are an AI news curator. Today is {TODAY}. Search the web for the latest AI developments from the past few days (this digest runs twice a week).

COVERAGE — go WIDE, not just the household names. Deliberately seek out news beyond OpenAI / Anthropic / Google:
- Frontier closed-source: OpenAI, Anthropic, Google Gemini, xAI/Grok, Microsoft (MAI), Meta, AND also Perplexity, Amazon (Nova), Cohere, Reka, AI21, Inflection, Mistral (commercial tier).
- Open-source / open-weight: DeepSeek, Qwen/Alibaba, Llama, Mistral, Gemma, Kimi/Moonshot, GLM/Zhipu, MiniMax, Phi, Falcon, Yi, InternLM, Nemotron/NVIDIA, OLMo/AI2, SmolLM/HuggingFace.
- Multimodal & specialized: VLMs, image/video gen (Stability, Black Forest Labs, Runway, Pika, Luma, Kling), speech/audio TTS/STT & voice (ElevenLabs, Suno, Udio, Cartesia), multilingual, code, math/reasoning, embeddings, robotics/world models.
- Research & innovation: notable arxiv papers, training techniques, new benchmarks, efficiency/quantization, agentic frameworks.
- Quick hits: funding, partnerships, policy, infra/chips.

PRIORITIZE RECORD-BREAKERS: any model that sets a NEW state-of-the-art or tops a leaderboard (SWE-bench, GPQA, AIME/MATH, MMLU-Pro, LMArena/Artificial Analysis, HumanEval, MMMU, VideoMME, etc.) — ESPECIALLY a smaller, cheaper, open-source, or lesser-known model beating a bigger/closed one. Lead with these and mark them ⭐.

The 📊 Comparative Snapshot section should NOT be empty: always include at least 2-3 lines on who currently leads which benchmark and any notable upsets.

CITATIONS (STRICT): Every item MUST end with a REAL clickable link in this EXACT form:
  <a href="https://REAL-URL-FROM-YOUR-SEARCH">[ShortLabel]</a>
where REAL-URL-FROM-YOUR-SEARCH is an actual URL you found via the web_search tool (e.g. the source article), and ShortLabel is a short publisher name like [Source], [Anthropic], [arxiv], [TechCrunch], [VentureBeat], [Microsoft].
- The visible text stays short (the bracket label); the href carries the full URL.
- NEVER output a bare bracket label like [Microsoft] WITHOUT a surrounding <a href="..."> — a bare label is useless because it isn't clickable.
- If you genuinely do not have a real URL for an item, OMIT the bracket entirely rather than leaving a dead label.

Mark surprising / counterintuitive findings with ⭐.

OUTPUT FORMAT — return ONLY a block of items, each tagged with a section. Use these EXACT delimiters and nothing else before/after:

===ITEMS===
@@SECTION: <one of: TLDR | FRONTIER | OPENSOURCE | MULTIMODAL | RESEARCH | COMPARATIVE | QUICKHITS>
@@HEADLINE: <short headline under 100 chars, no HTML>
@@HTML: <a single-line HTML <li>...</li> with the description and an inline [Source] link>
(repeat the three @@ lines for each item)
===END===

Rules:
- TLDR section = 3-5 of the single biggest NEW headlines (these also appear in their topical section).
- Put each substantive item under its best-fit topical section.
- Keep each @@HTML on ONE line (no line breaks inside it).
- Aim for breadth: 14-22 items total across sections, spanning MANY different labs (not 3 items all about OpenAI).
- Only include genuinely recent developments (past few days since the last twice-weekly run)."""

    print("[claude] gathering candidates (Haiku + web_search)")
    msg = anthropic.messages.create(
        model="claude-haiku-4-5",
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Gather today's ({TODAY}) AI news candidate items. Search thoroughly and return the ===ITEMS=== block."}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
    )

    # Log the actual web_search queries Claude issued this run (visible in CloudWatch).
    # These are chosen dynamically by the model each run; max_uses=8 is just the ceiling.
    queries = [
        b.input.get("query")
        for b in msg.content
        if getattr(b, "type", "") == "server_tool_use"
        and getattr(b, "name", "") == "web_search"
        and isinstance(getattr(b, "input", None), dict)
    ]
    print(f"[search] {len(queries)} web searches issued (cap 8):")
    for i, q in enumerate(queries, 1):
        print(f"         {i}. {q}")

    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    print(f"[claude] response length: {len(text)} chars, stop_reason: {msg.stop_reason}")

    s = text.find("===ITEMS===")
    e = text.find("===END===")
    block = text[s + len("===ITEMS==="):(e if e != -1 else len(text))] if s != -1 else text

    items = []
    cur = {}
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith("@@SECTION:"):
            if cur.get("headline") and cur.get("html"):
                items.append(cur)
            cur = {"section": line[len("@@SECTION:"):].strip().upper()}
        elif line.startswith("@@HEADLINE:"):
            cur["headline"] = line[len("@@HEADLINE:"):].strip()
        elif line.startswith("@@HTML:"):
            cur["html"] = line[len("@@HTML:"):].strip()
    if cur.get("headline") and cur.get("html"):
        items.append(cur)

    # Normalize sections + sanitize dead citation labels
    for it in items:
        if it.get("section") not in VALID_SECTIONS:
            it["section"] = "QUICKHITS"
        it["html"] = _sanitize_citations(it["html"])

    if not items:
        print(f"[error] no items parsed. raw head: {text[:800]}")
        raise ValueError("Claude returned no parseable items")

    print(f"[claude] parsed {len(items)} candidate items")
    return items


# ---------------- Phase 2: semantic dedup ----------------
def dedup(candidates: list, stored: list) -> tuple:
    headlines = [c["headline"] for c in candidates]
    vectors = embed(headlines)
    for c, v in zip(candidates, vectors):
        c["vector"] = v

    survivors = []
    dropped = []
    seen_vectors = [v for _, v in stored]  # grows as we accept within-batch too

    for c in candidates:
        best = 0.0
        for sv in seen_vectors:
            sim = _cosine(c["vector"], sv)
            if sim > best:
                best = sim
            if best >= SIMILARITY_THRESHOLD:
                break
        if best >= SIMILARITY_THRESHOLD:
            dropped.append((c["headline"], round(best, 3)))
        else:
            survivors.append(c)
            seen_vectors.append(c["vector"])  # dedup within the same batch too

    print(f"[dedup] {len(survivors)} kept, {len(dropped)} dropped (threshold {SIMILARITY_THRESHOLD})")
    for h, s in dropped:
        print(f"        drop ~{s}: {h[:70]}")
    return survivors, dropped


# ---------------- Phase 3: assemble email (pure Python) ----------------
def assemble(survivors: list) -> tuple:
    by_section = {k: [] for k, _ in SECTIONS}
    tldr = []
    for c in survivors:
        sec = c["section"]
        if sec == "TLDR":
            tldr.append(c)
        elif sec in by_section:
            by_section[sec].append(c)

    # If model didn't tag TLDR, synthesize from first few survivors
    if not tldr:
        tldr = survivors[:5]

    html = [f"<h2>🤖 Weekly AI Digest — {TODAY}</h2>"]
    html.append("<h3>⚡ TL;DR</h3><ul>")
    for c in tldr:
        html.append(c["html"])
    html.append("</ul><hr>")

    for key, header in SECTIONS:
        html.append(f"<h3>{header}</h3><ul>")
        rows = by_section[key]
        if rows:
            for c in rows:
                html.append(c["html"])
        else:
            html.append("<li><em>No notable new developments today.</em></li>")
        html.append("</ul>")

    html.append("<hr><p><em>Curated for you by Claude | AWS Lambda Scheduled Agent</em></p>")
    html_body = "\n".join(html)

    # Plain-text version (reuse module-level _re; no per-call import)
    plain_lines = [f"Weekly AI Digest - {TODAY}", "", "TL;DR:"]
    for c in tldr:
        plain_lines.append("- " + _re.sub(r"<[^>]+>", "", c["html"]).strip())
    for key, header in SECTIONS:
        plain_lines.append("")
        plain_lines.append(_re.sub(r"<[^>]+>", "", header).strip().upper())
        rows = by_section[key]
        if rows:
            for c in rows:
                plain_lines.append("- " + _re.sub(r"<[^>]+>", "", c["html"]).strip())
        else:
            plain_lines.append("- No notable new developments today.")
    plain_lines.append("")
    plain_lines.append("Curated by Claude | AWS Lambda Scheduled Agent")
    return html_body, "\n".join(plain_lines)


# ---------------- Email senders ----------------
def send_email(html_body: str, plain_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🤖 Weekly AI Digest — {TODAY}"
    msg["From"] = f"Claude AI Digest <{SENDER}>"
    msg["To"] = RECIPIENT_HEADER
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(SENDER, GMAIL_APP_PASSWORD)
        server.sendmail(SENDER, RECIPIENTS, msg.as_bytes())  # list = deliver to all
    print(f"[ok] email sent to {RECIPIENT_HEADER}")


def send_alert(subject: str, body_text: str) -> None:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Claude AI Digest <{SENDER}>"
        msg["To"] = RECIPIENT_HEADER
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(SENDER, RECIPIENTS, msg.as_bytes())
        print(f"[alert] sent: {subject}")
    except Exception as e:
        print(f"[alert-failed] {e}")


def _classify_error(err: Exception) -> tuple:
    text = f"{type(err).__name__}: {err}".lower()
    raw = f"{type(err).__name__}: {err}"
    if any(k in text for k in ["credit balance", "insufficient", "billing", "payment required", "quota"]):
        return ("⚠️ AI Digest FAILED — Anthropic API credit exhausted",
                f"Your AI Digest run on {TODAY} did NOT complete.\n\nREASON: Anthropic API credit appears exhausted.\n\nACTION: Recharge at https://console.anthropic.com/settings/billing\nThe next scheduled run resumes automatically once topped up.\n\n--- detail ---\n{raw}\n")
    if "rate" in text and "limit" in text:
        return ("⚠️ AI Digest FAILED — Anthropic rate limit hit",
                f"Your AI Digest run on {TODAY} did NOT complete (rate limit). Usually temporary; next run retries.\n\n--- detail ---\n{raw}\n")
    if any(k in text for k in ["authentication", "invalid x-api-key", "unauthorized", "401"]):
        return ("⚠️ AI Digest FAILED — API key invalid",
                f"Your AI Digest run on {TODAY} did NOT complete (auth). Check/rotate the key in SSM (/ai-digest/*).\n\n--- detail ---\n{raw}\n")
    return ("⚠️ AI Digest FAILED — unexpected error",
            f"Your AI Digest run on {TODAY} did NOT complete.\n\n--- detail ---\n{raw}\n")


# ---------------- Lambda entrypoint ----------------
def handler(event, context):
    _refresh_today()  # guard against stale date on warm-container reuse
    print(f"[start] AI Digest for {TODAY}")
    try:
        stored = get_stored_embeddings()
        print(f"[dedup] {len(stored)} stored embeddings loaded")

        candidates = gather_candidates()
        survivors, dropped = dedup(candidates, stored)

        html_body, plain_body = assemble(survivors)
        send_email(html_body, plain_body)

        if survivors:
            record_items([{"headline": c["headline"], "vector": c["vector"]} for c in survivors])
            print(f"[ddb] recorded {len(survivors)} embeddings")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "date": TODAY_KEY,
                "candidates": len(candidates),
                "kept": len(survivors),
                "dropped_duplicates": len(dropped),
                "stored_compared": len(stored),
            }),
        }
    except Exception as err:
        print(f"[FATAL] {type(err).__name__}: {err}")
        subject, body = _classify_error(err)
        send_alert(subject, body)
        raise


if __name__ == "__main__":
    print(handler({}, None))
