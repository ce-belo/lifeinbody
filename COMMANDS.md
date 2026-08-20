# Life in Body — Commands Reference

Every CLI command this app supports, when to use each one, and which ones cost real money (Anthropic API calls).

> Tip: `lifeinbody <command> --help` shows the same info Typer auto-generates. This file adds *when* and *why* — the flags themselves are reproduced for convenience.

---

## Daily workflow

The common path on a normal morning:

```
lifeinbody dashboard              # sync sheet + emails, open the dashboard in the browser
lifeinbody draft --all-pending    # (optional) generate AI replies for threads needing follow-up
lifeinbody draft --nudge-waiting  # (optional) draft polite check-ins for threads where they've gone silent
lifeinbody review                 # walk through drafts, accept / regenerate / skip
```

Then approve and send the drafts you like inside Gmail itself — the app never sends email on your behalf.

**Cost shorthand**

| Symbol | Meaning |
|---|---|
| 💰 | Calls the Anthropic API — costs real money per call |
| 🆓 | Local-only or Google APIs — no API cost |

---

## All commands

### `lifeinbody dashboard` 🆓

Sync sheet + emails, then build and open the HTML business dashboard.

Sync errors are reported but don't abort the render — a partial dashboard beats no dashboard. **LLM classification is intentionally skipped** to keep costs predictable; run `sync emails --llm-classify` or `draft` on demand if you want AI work done.

Flags:
- `--no-sync` — Skip the sheet + email sync; just re-render `dashboard.html` from cached data
- `--no-open` — Render the HTML but don't open it in a browser
- `--debug` — Verbose logs to `data/lifeinbody.log`

When to use: **first thing in the morning** to see fresh KPIs and inbox state.

---

### `lifeinbody draft [thread_id]` 💰

Generate a Gmail draft for one thread, every pending thread, or every waiting thread.

Each draft call costs about **$0.03** (Sonnet 4.6 at $3/$15 per 1M tokens for a typical ~5K input / ~1K output draft); nudges are roughly half that since the output is shorter. Drafts go to your Gmail **Drafts** folder — never auto-sent.

Two modes:
- **reply** (default) — answers the latest message in a `status='new'` thread (ball is in your court).
- **nudge** — writes a short check-in for a `status='waiting'` thread (you sent last, they went silent).

Arguments:
- `thread_id` (optional) — Gmail thread ID to draft for

Flags:
- `--all-pending` — Reply-draft every `status='new'` thread that doesn't already have a draft.
- `--nudge-waiting` — Nudge-draft every `status='waiting'` thread that doesn't already have a draft.
- `--nudge` — Treat the supplied `thread_id` as a nudge rather than a reply. Single-thread only; use `--nudge-waiting` for the bulk flow.
- `--debug` — Verbose logs

`--all-pending` and `--nudge-waiting` are mutually exclusive, and neither can be combined with a `thread_id`.

When to use:
- `--all-pending` — first thing after `dashboard`, to clear the "Need reply" queue.
- `--nudge-waiting` — when "Waiting on their reply" has grown and you want polite check-ins for everyone you're chasing.

---

### `lifeinbody review` 💰 (regenerations only)

Walk every thread needing follow-up. For each one: accept the existing draft, regenerate a new one (💰 each regenerate ≈ $0.03), edit the tone hint, or skip.

Flags:
- `--debug` — Verbose logs

When to use: triage flow once `draft --all-pending` has run, or to iterate on draft tone.

---

### `lifeinbody sync emails` 🆓 or 💰

Pull new Gmail threads into the local SQLite tracker.

Flags:
- `--labels TEXT` — Comma-separated Gmail labels to sync (default: `INBOX`)
- `--llm-classify` 💰 — Run the LLM second-pass classifier on threads the rules engine couldn't decide. Adds **~$0.005 per new thread** (Sonnet 4.6).
- `--debug` — Log message bodies + extra detail

When to use:
- Without `--llm-classify`: fast, free email refresh (rules-based classification only).
- With `--llm-classify`: deeper triage — useful when the rules engine is too conservative and you want AI to second-guess "does this thread actually need a reply?"

Note: `dashboard` already runs this command (without `--llm-classify`) on every open.

---

### `lifeinbody sync sheet` 🆓

Pull the operations Google Sheet into a cached snapshot at `data/snapshot.json`.

Flags:
- `--debug` — Verbose logs

When to use: standalone refresh of sheet data without rebuilding the dashboard. `dashboard` runs this for you on every open.

---

### `lifeinbody summary` 💰 (only with `--email`)

Daily summary of inbox state + business KPIs, printed to the terminal.

Flags:
- `--json` — Emit machine-readable JSON (no terminal coloring; pipeable)
- `--email` — Also drop a Gmail draft of the summary for `DAILY_SUMMARY_RECIPIENT` from `.env`. The draft body itself is deterministic Python (free); only the Gmail API call happens — but the rest of the path is free too.
- `--debug` — Verbose logs

When to use: quick terminal snapshot of the same data the dashboard shows, without opening the browser.

---

### `lifeinbody run-daily` 💰

> ⚠️ **Legacy.** This was the all-in-one command run by the retired 7am `launchd` job. It still works (sync emails → sync sheet → refresh dashboard → email summary draft) but is not the recommended path anymore. Prefer the daily-workflow commands at the top of this file.

Flags:
- `--debug` — Verbose logs

When to use: if you ever want a one-shot "do everything" command — for example, before going on vacation. Otherwise ignore.

---

### `lifeinbody auth` 🆓

One-time Gmail OAuth — authorize `team@lifeinbody.com`.

Flags:
- `--force` — Re-run OAuth even if a valid token exists

When to use:
- First time setup
- After Google revokes the token (rare — happens if you change passwords or revoke the app)
- When `auth status` reports the authenticated account doesn't match `LIFEINBODY_GMAIL_ADDRESS` in `.env`

---

## How thread status works

Every thread in the local DB carries a `status`:

| Status | Meaning |
|---|---|
| `new` | Last message from them, awaiting our first reply |
| `replied` | We replied recently (≤ 3 business days ago) |
| `waiting` | We replied > 3 business days ago, no response from them yet |
| `closed` | Thread is in the **"closed"** Gmail label |

**Move a thread to the "closed" label in Gmail** when it's resolved — paid invoice, answered question, no follow-up needed. The next sync will pick it up, the classifier will mark it `closed`, and it'll drop out of the dashboard's active counts.

The dashboard's three "action" counts are:

- **Open threads** — everything not closed. The total in brackets shows how many you've closed historically.
- **Need reply (new)** — threads where someone wrote you and you haven't replied yet (and have no approved draft for).
- **Waiting on their reply (>3 biz days)** — threads where you sent the last message and it's been more than 3 business days with no response. This catches the case where someone partially answered, you followed up, and they went silent.

Together, **Need reply + Waiting on their reply = "things requiring your attention right now"**.

---

## Cost tracking

The dashboard shows a **Claude API spend** card at the bottom with two numbers:

- **This month** — calendar-month total in USD + call count
- **All time** — total since the first recorded call

Numbers are computed from real `usage` data returned by Anthropic on every call (input tokens × $3/1M + output tokens × $15/1M for Sonnet 4.6), not estimates. They will not include any usage you generated before this tracking was added.

The raw rows live in the `llm_usage` table in `data/lifeinbody.db` if you want to query them directly — e.g. cost by operation:

```sh
sqlite3 data/lifeinbody.db "SELECT operation, COUNT(*), ROUND(SUM(cost_usd), 4) FROM llm_usage GROUP BY operation"
```

---

## Files this app writes

| Path | Contents |
|---|---|
| `data/lifeinbody.db` | SQLite — threads, messages, drafts, invoices, **llm_usage** |
| `data/snapshot.json` | Cached Google Sheet snapshot (rewritten on every `sync sheet`) |
| `data/dashboard.html` | The rendered dashboard (rewritten on every `dashboard`) |
| `data/lifeinbody.log` | Debug log when any command runs with `--debug` |
| `credentials/*.json` | OAuth client + token (gitignored — never commit) |
| `.env` | API keys and config (gitignored — never commit) |

Nothing under `data/` or `credentials/` should ever be committed.
