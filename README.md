# 🤖 Daily AI Digest — AWS Lambda

A serverless agent that searches the web for the latest AI news, **semantically deduplicates**
against recent history, and emails a formatted digest. Runs on **AWS Lambda**, triggered by
**EventBridge** cron, with **DynamoDB** for embedding-based dedup memory and **SSM Parameter
Store** for secrets.

- **Model:** Claude Haiku 4.5 (the `web_search` tool does the content heavy-lifting; Haiku formats)
- **Dedup:** OpenAI `text-embedding-3-small` @ **512 MRL dims** + cosine similarity, threshold **0.80**
- **Schedule:** Monday & Friday at 9:00 AM IST (3:30 AM UTC)
- **Delivery:** Gmail SMTP → `attili.sanjeet10@gmail.com`
- **Secrets:** AWS SSM Parameter Store (`/ai-digest/*`, SecureString)
- **Region:** `ap-south-1` (Mumbai) · **Stack:** `ai-daily-digest`

---

## 📐 Architecture

```
EventBridge cron (Mon & Fri, 3:30 UTC)
        │
        ▼
   AWS Lambda (ai-digest-daily, Python 3.13)
        │
        ├─►  SSM Parameter Store (/ai-digest/*)   →  fetch secrets at cold start
        │
        ├─►  PHASE 1  Claude Haiku + web_search    →  gather section-tagged candidate items
        ├─►  PHASE 2  OpenAI embeddings (512-dim)  →  cosine vs stored; drop sim ≥ 0.80
        ├─►  PHASE 3  Python templating            →  assemble HTML/plain email (no 2nd LLM call)
        │
        ├─►  DynamoDB (ai-digest-sent-items)       →  store survivors' embeddings (14-day TTL)
        └─►  Gmail SMTP (smtp.gmail.com:587)        →  send the digest email
```

Provisioned as a single **CloudFormation stack** via **AWS SAM**.

### Why this pipeline
- **Search > model size:** the web_search tool supplies the facts, so cheap Haiku is enough for
  formatting → ~$0.13/run instead of ~$0.50 with Sonnet.
- **Embedding dedup > prompt dedup:** semantic similarity catches paraphrased repeats (e.g. two
  different articles about the same launch) that a text-list-in-the-prompt approach would miss —
  and it keeps the prompt small (no growing headline list = no context creep).
- **MRL 512 dims:** OpenAI embeddings are Matryoshka-trained; 512 is a native breakpoint
  ({512,1024,1536,3072}), so we keep semantic quality at ⅓ the storage of full 1536-dim.

---

## 🗂️ Project Structure

```
jobs/ai-digest/
├── README.md             # this file
├── template.yaml         # SAM blueprint (Lambda + DynamoDB + EventBridge + SSM/KMS IAM)
├── tools/
│   └── calibrate_threshold.py   # offline experiment to tune the similarity threshold
└── src/
    ├── app.py            # Lambda handler: secrets → gather → embed/dedup → assemble → email
    └── requirements.txt  # anthropic, boto3  (OpenAI called via plain HTTP, no SDK)
```

---

## ✅ Prerequisites (one-time)

| Tool | Install | Verify |
|------|---------|--------|
| **AWS CLI v2** | [AWSCLIV2.msi](https://awscli.amazonaws.com/AWSCLIV2.msi) or `winget install -e --id Amazon.AWSCLI` | `aws --version` |
| **AWS SAM CLI** | [AWS_SAM_CLI_64_PY3.msi](https://github.com/aws/aws-sam-cli/releases/latest/download/AWS_SAM_CLI_64_PY3.msi) or `winget install -e --id Amazon.SAM-CLI` | `sam --version` |
| **Python 3.13** | [python.org](https://www.python.org/downloads/) | `python --version` |

> After installing, **fully restart the terminal / app** so PATH refreshes.

### AWS credentials
```bash
aws configure        # region: ap-south-1, output: json
aws sts get-caller-identity   # verify
```

### Secrets live in SSM Parameter Store (not env vars, not code)

| SSM parameter | Value | Where to get it |
|---------------|-------|-----------------|
| `/ai-digest/anthropic-api-key` | `sk-ant-...` | https://console.anthropic.com → API Keys |
| `/ai-digest/gmail-app-password` | 16-char app password | Gmail → Security → 2-Step → App passwords |
| `/ai-digest/openai-api-key` | `sk-proj-...` | https://platform.openai.com → API keys (for embeddings) |

Create / update them as **SecureString** (KMS-encrypted):
```powershell
aws ssm put-parameter --name "/ai-digest/anthropic-api-key"  --type SecureString --overwrite --region ap-south-1 --value "sk-ant-..."
aws ssm put-parameter --name "/ai-digest/gmail-app-password" --type SecureString --overwrite --region ap-south-1 --value "xxxxxxxxxxxxxxxx"
aws ssm put-parameter --name "/ai-digest/openai-api-key"     --type SecureString --overwrite --region ap-south-1 --value "sk-proj-..."

# List them
aws ssm get-parameters-by-path --path "/ai-digest/" --region ap-south-1 --query "Parameters[*].Name" --output text
```
The Lambda reads them at cold start via `ssm:GetParameter` (IAM scoped to `/ai-digest/*` + `kms:Decrypt`).

> 🔄 **Rotating a key = just update the SSM param** (command above). No redeploy, no code change.
> The next cold start picks it up.

---

## 🚀 Deploy (first time & every update)

Run from `jobs/ai-digest/`.

> ⚠️ **OneDrive note:** this folder lives in OneDrive, which locks files mid-upload. Build to a
> non-synced dir (`--build-dir %LOCALAPPDATA%\...`) to avoid `Permission denied ... .gz` errors.

```powershell
# 1. Build
$env:SAM_CLI_TELEMETRY = "0"
sam build --build-dir "$env:LOCALAPPDATA\ai-digest-build"

# 2. Deploy   (NOTE: no secrets on the command line — they come from SSM)
sam deploy `
  --template-file "$env:LOCALAPPDATA\ai-digest-build\template.yaml" `
  --stack-name ai-daily-digest `
  --region ap-south-1 `
  --capabilities CAPABILITY_IAM `
  --resolve-s3 `
  --no-confirm-changeset `
  --no-fail-on-empty-changeset
```

> ⚠️ **Changing a template Parameter** (e.g. `SimilarityThreshold`, `EmbedDims`): CloudFormation
> **keeps the previous value** unless you pass it explicitly. To actually change it, add e.g.
> `--parameter-overrides "SimilarityThreshold=0.80"`. Verify afterwards:
> ```powershell
> aws lambda get-function-configuration --function-name ai-digest-daily --region ap-south-1 --query "Environment.Variables"
> ```

**What gets created** (CloudFormation):
1. **IAM Role** — DynamoDB CRUD + read `/ai-digest/*` SSM + `kms:Decrypt`
2. **DynamoDB Table** — `ai-digest-sent-items` (embedding dedup memory)
3. **Lambda Function** — `ai-digest-daily`
4. **EventBridge Rule** — cron trigger (from the `Events:` block)
5. **Lambda Permission** — lets EventBridge invoke the function

---

## 🧪 Test manually (don't wait for cron)

```powershell
aws lambda invoke `
  --function-name ai-digest-daily --region ap-south-1 `
  --invocation-type RequestResponse --cli-read-timeout 700 `
  --payload "{}" --cli-binary-format raw-in-base64-out `
  "$env:LOCALAPPDATA\digest-out.json"

Get-Content "$env:LOCALAPPDATA\digest-out.json"
```

Successful response:
```json
{"statusCode": 200, "body": "{\"date\": \"2026-06-07\", \"candidates\": 18, \"kept\": 18, \"dropped_duplicates\": 0, \"stored_compared\": 0}"}
```
…and the digest email arrives in the inbox.

---

## 🔍 Debugging — logs

```powershell
$lg = "/aws/lambda/ai-digest-daily"
$stream = aws logs describe-log-streams --log-group-name $lg --region ap-south-1 `
  --order-by LastEventTime --descending --query "logStreams[0].logStreamName" --output text
aws logs get-log-events --log-group-name $lg --log-stream-name "$stream" `
  --region ap-south-1 --query "events[*].message" --output text
```
Or live: `sam logs --stack-name ai-daily-digest --region ap-south-1 --tail`

**Healthy log markers:**
```
[start] AI Digest for ...
[dedup] N stored embeddings loaded
[claude] gathering candidates (Haiku + web_search)
[claude] parsed M candidate items
[dedup] K kept, D dropped (threshold 0.8)
        drop ~0.97: <headline>            ← a caught duplicate
[ok] email sent to ...
[ddb] recorded K embeddings
```
**On failure** a `[FATAL]` line appears and a classified **alert email** is sent (see below).

---

## 🗄️ DynamoDB dedup store

```powershell
# Non-embedding fields (avoids console emoji crash); embeddings are large binary blobs
aws dynamodb scan --table-name ai-digest-sent-items --region ap-south-1 `
  --projection-expression "item_key, #d, expires_at" `
  --expression-attribute-names '{\"#d\":\"date\"}' --max-items 5 --output json

aws dynamodb scan --table-name ai-digest-sent-items --region ap-south-1 --select COUNT
```

### Schema

| Field | Type | Size | Purpose |
|-------|------|------|---------|
| `item_key` | String (PK) | ~24 B | SHA-256 (first 24 chars) of lowercased headline |
| `headline` | String | ~80 B | Headline text (debug / readability) |
| `embedding` | **Binary** | **2,048 B** | 512 × float32 MRL vector for cosine similarity |
| `date` | String | ~10 B | `YYYY-MM-DD` sent (IST) |
| `expires_at` | Number | ~8 B | Unix ts; **TTL auto-deletes the row after 14 days** |

**TTL = rolling 14-day memory.** Each row self-deletes ~14 days after creation (AWS lag ≤48h). The
table plateaus at ~120 items (~264 KB) — well inside the DynamoDB free tier, so storage cost ≈ $0.

> ⚠️ **Never mix embedding dimensions.** If you change `EmbedDims`, **flush the table** first so old
> vectors of a different size don't get cosine-compared against new ones (zip truncates → garbage).
> Flush:
> ```powershell
> $keys = aws dynamodb scan --table-name ai-digest-sent-items --region ap-south-1 --projection-expression "item_key" --query "Items[*].item_key.S" --output text
> foreach ($k in ($keys -split "\s+")) { if ($k) { aws dynamodb delete-item --table-name ai-digest-sent-items --region ap-south-1 --key "{\`"item_key\`":{\`"S\`":\`"$k\`"}}" } }
> ```

---

## 🎯 Tuning the similarity threshold

The threshold (default **0.80**) was calibrated with `tools/calibrate_threshold.py`, which embeds
controlled headline pairs and prints cosine scores:

```powershell
$env:PYTHONUTF8 = "1"
python tools/calibrate_threshold.py
```

Calibration finding: true paraphrase duplicates score **0.84–0.95**, while *hard negatives* (same
entity, different news — e.g. "Gemini Flash" vs "Gemini Pro") top out at **~0.717**. So **0.80**
catches real duplicates with an ~0.08 safety margin and **zero** false merges. Going lower risks
merging legitimately different news; going higher (0.90) misses real paraphrases.

To change it: re-deploy with `--parameter-overrides "SimilarityThreshold=0.80"` (and verify, per the
deploy note above).

---

## 🚨 Failure alerts

The handler wraps everything in error handling; on failure it emails a classified alert:

| Cause detected | Email subject |
|----------------|---------------|
| Credit / billing exhausted | ⚠️ AI Digest FAILED — Anthropic API credit exhausted (+ recharge link) |
| Rate limit | ⚠️ AI Digest FAILED — Anthropic rate limit hit (auto-retries next run) |
| Auth / invalid key | ⚠️ AI Digest FAILED — API key invalid (check SSM `/ai-digest/*`) |
| Anything else | ⚠️ AI Digest FAILED — unexpected error (+ technical detail) |

So budget exhaustion is never a silent failure — you get a mail telling you to recharge at
`https://console.anthropic.com/settings/billing`.

---

## ⚙️ Common changes

| Change | How |
|--------|-----|
| **Schedule** | `template.yaml` → `Schedule:` (AWS cron, 6 fields, UTC). Redeploy. |
| **Model** | `src/app.py` → `model="claude-haiku-4-5"` (e.g. `claude-sonnet-4-5` for richer output). |
| **# web searches / cost** | `src/app.py` → `max_uses` in the `tools=[...]` block. |
| **Similarity threshold** | redeploy with `--parameter-overrides "SimilarityThreshold=0.80"`. |
| **Embedding dims** | `template.yaml` `EmbedDims` (use a 512/1024/1536 MRL breakpoint), redeploy with override, **flush table**. |
| **Rotate a secret** | `aws ssm put-parameter --overwrite ...` — no redeploy needed. |

Cron examples:
```yaml
Schedule: 'cron(30 3 ? * MON,FRI *)'   # 9 AM IST, Mon & Fri  (current)
Schedule: 'cron(30 3 * * ? *)'         # 9 AM IST, every day
Schedule: 'cron(30 3 ? * MON-FRI *)'   # 9 AM IST, weekdays
```
IST = UTC + 5:30, so 9:00 IST → 3:30 UTC.

---

## 💰 Cost

| Item | Notes |
|------|-------|
| Lambda / EventBridge / DynamoDB / SSM (Standard) | Free tier covers all of it (~$0) |
| OpenAI embeddings | ~$0.00002/run (negligible) |
| Anthropic API (Haiku + 5 searches) | ~$0.13–0.16/run → **~$1.20/month** at Mon/Fri |

**A $5 Anthropic balance lasts ~4 months.** If it runs out you get the credit-exhausted alert email.

---

## 🧹 Tear down

```bash
sam delete --stack-name ai-daily-digest --region ap-south-1
```
Removes Lambda, EventBridge rule, IAM role, and DynamoDB table. (SSM params and the shared SAM S3
bucket are left in place — delete SSM params manually if desired.)

---

## 📌 Quick reference

```bash
# Build
sam build --build-dir "%LOCALAPPDATA%\ai-digest-build"

# Deploy (secrets come from SSM; add --parameter-overrides only to change a Parameter)
sam deploy --template-file "%LOCALAPPDATA%\ai-digest-build\template.yaml" \
  --stack-name ai-daily-digest --region ap-south-1 \
  --capabilities CAPABILITY_IAM --resolve-s3 \
  --no-confirm-changeset --no-fail-on-empty-changeset

# Test
aws lambda invoke --function-name ai-digest-daily --region ap-south-1 \
  --payload "{}" --cli-binary-format raw-in-base64-out out.json

# Logs / Delete
sam logs --stack-name ai-daily-digest --region ap-south-1 --tail
sam delete --stack-name ai-daily-digest --region ap-south-1
```
