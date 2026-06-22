# Life in Body — Secretary + Business Dashboard

A local Python tool that pairs a Gmail secretary agent (drafts replies for `team@lifeinbody.com`, never auto-sends) with a business dashboard built off the Life in Body operations Google Sheet (read-only). Same codebase, same config, one CLI.

## How to run

```bash
# 1. Set up a virtualenv and install
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Copy and fill in secrets
cp .env.example .env
# edit .env

# 3. Drop your Google OAuth client JSON at credentials/oauth_client.json
#    (see Step 2 walkthrough)

# 4. Authenticate once, then sync + summarize
lifeinbody auth
lifeinbody sync emails
lifeinbody sync sheet
lifeinbody summary
lifeinbody dashboard
```

All secrets and local data live in `credentials/` and `data/` — both gitignored. Drafts are written to Gmail Drafts only; the user approves and sends in Gmail. The Google Sheet is never written to.

## Setting up Gmail OAuth (one-time)

1. **Create a project** at https://console.cloud.google.com (signed in as the inbox owner — e.g. `team@lifeinbody.com`). Name it `lifeinbody-secretary`.
2. **Enable the Gmail API** under APIs & Services → Library.
3. **OAuth consent screen**: User Type **External**, Publishing status **Testing**. Add these three scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
   - `https://www.googleapis.com/auth/gmail.labels`
   Add `team@lifeinbody.com` (and any other inbox you'll authenticate) under **Test users**.
4. **Credentials → + Create Credentials → OAuth client ID**, type **Desktop app**. Download the JSON.
5. Save the JSON as `credentials/oauth_client.json`.
6. Run `lifeinbody auth`. A browser opens, you sign in as the inbox owner, approve the three scopes (Google will warn "unverified app" — proceed; you're the developer). The refresh token is saved to `credentials/token.json`.

Re-run with `lifeinbody auth --force` to delete the saved token and re-authorize (e.g. if you signed in as the wrong account).
