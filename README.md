# MEXC futures breakout scanner

Scans MEXC USDT perps every 15 minutes via GitHub Actions and pushes matching
breakouts to Telegram with a direct futures link. Read-only — no MEXC API key.

## Filter

All conditions must hold on the most recently closed 15m bar:

| # | Condition |
|---|---|
| 1 | 24h price change ≥ +15% |
| 2 | Last 15m bar quote volume ≥ $200k USDT |
| 3 | Last bar volume > previous bar volume |
| 4 | Last bar volume > 2× 20-bar average volume |
| 5 | Close > rolling 96-bar high (24h breakout) |
| 6 | 4-bar ROC > 0 AND accelerating |
| 7 | Previous bar was NOT already above the 24h high (alert on breakout, not continuation) |

Adjust thresholds at the top of `scanner.py`.

## One-time setup

### 1. Create the Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → pick a name → save the **bot token** it gives you.
2. Search for your new bot in Telegram and click **Start** (this lets it message you).
3. Message **@userinfobot** to get your numeric **chat ID** (or send any message to your bot then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to read it).

### 2. Push this folder to GitHub

```bash
cd mexc_alerts
git init
git add .
git commit -m "initial commit"
gh repo create mexc-alerts --private --source=. --push
```

(Or create the repo through the GitHub UI and push manually.)

### 3. Add the secrets

In the repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | your numeric chat ID |

### 4. Enable the workflow

GitHub disables scheduled workflows by default on new repos. Go to **Actions** tab, accept the prompt, and either wait for the next 15-minute boundary or hit **Run workflow** on `MEXC futures scan` for an immediate test run.

## Local test before pushing

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
pip install -r requirements.txt
python scanner.py
```

On Windows PowerShell:
```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
pip install -r requirements.txt
python scanner.py
```

A successful run prints how many candidates were screened, how many signals fired, and whether each Telegram send succeeded.

## Known limitations

- **GitHub Actions cron is best-effort.** Runs can be delayed up to ~30 minutes during high load. If signal freshness matters more than free hosting, move to a $5/mo VPS with a cron job or systemd timer.
- **Rate limits.** MEXC's swap endpoint rate-limits aggressively. The scanner pre-filters by 24h % change before deep-fetching 15m OHLCV, so only ~10–50 candidates get the deep pull on a normal day. If your pre-filter is too loose and you hit `code 510 Requests are too frequent`, increase `MIN_24H_QV_PREFILTER` or lower `WORKERS` in `scanner.py`.
- **Dedupe is per-breakout, not per-pair.** A pair that breaks out, pulls back below the 24h high, then breaks out again WILL alert twice. Usually fine — those are genuinely two different setups.
