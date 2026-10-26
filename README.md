# ServerHive giveaway watch

Tracks the ServerHive giveaway sources and sends an [ntfy](https://ntfy.sh) push
when something appears:

- **@ServerHive** public channel — every new post is checked. The channel is
  low-volume, so new posts alert even when they are just a teaser. Giveaway
  keywords or a `giveaway.serverhive.in` link raise the priority.
- **@ServerRentals** group — every new message from **@Shra1V32**, plus any
  member message containing the giveaway URL. This uses a Telethon user session
  because `@ServerRentals` is a group, not a broadcast channel, so it cannot be
  read from the public `t.me/s/...` preview.
- **giveaway.serverhive.in** — alerts when the page changes from an error
  (currently Cloudflare `502`) to a reachable response, when its visible content
  changes while live, or when it goes down again.
- **Watcher health** — repeated source failures, an expired Telegram session, or
  a missing group membership produce an alert instead of failing silently.

The first successful run sends a "watcher started" notification and baselines
the current channel/site state, so old posts are not replayed. A giveaway post
from the previous 48 hours, a recent message from @Shra1V32 (previous 6 hours),
or a currently-live giveaway page is still flagged on that first run.

## How it runs

`watcher.py` is a single-shot program. `.github/workflows/watch.yml` runs it
every 5 minutes on GitHub Actions, then caches `.state.json` between runs. The
state stores only message IDs, timestamps, HTTP status, and content hashes — not
message text.

If the Telegram secrets are not configured, the group check is skipped and the
public channel/site checks still run. This makes the setup testable before the
Telegram login is finished.

## Setup

### 1. Subscribe to ntfy

Install the ntfy app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
[iOS](https://apps.apple.com/us/app/ntfy/id1625396347), or the web app) and
subscribe to the topic stored in the repository's `NTFY_TOPIC` secret.

The topic value is deliberately not in this public repository. It was generated
when the repo was created and is stored in
`~/Dev/serverhive-watch/.setup.local.md` on that machine.
Treat it like a password: anyone with the topic can read the alerts. To rotate
it:

```bash
gh secret set NTFY_TOPIC -R greed-d/serverhive-watch
```

### 2. Create Telegram API credentials

1. Sign in at <https://my.telegram.org/apps> with the account that is a member
   of **@ServerRentals** (join the group first if needed).
2. Create an application. Copy the **api_id** and **api_hash**.
3. Do not use a bot token. A bot is not used here because the group did not
   allow adding one; reading `@ServerRentals` requires a user session.

### 3. Generate a Telethon session

```bash
cd ~/Dev/serverhive-watch
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python login.py
```

`login.py` asks for the api_id/api_hash, then Telethon asks for your phone
number (international format), the login code, and your 2FA password if enabled.
It prints a `TG_SESSION_STRING`.

**The session string grants full access to your Telegram account.** Store it
only as a GitHub Actions secret. Never commit it, paste it into an issue, or put
it in a shell command with `-b`.

### 4. Add the GitHub secrets

```bash
gh secret set TG_API_ID -R greed-d/serverhive-watch
gh secret set TG_API_HASH -R greed-d/serverhive-watch
gh secret set TG_SESSION_STRING -R greed-d/serverhive-watch
```

Paste each value at the prompt. Confirm the names (not the values):

```bash
gh secret list -R greed-d/serverhive-watch
```

### 5. Test

```bash
gh workflow run watch.yml -R greed-d/serverhive-watch
RUN_ID=$(gh run list -R greed-d/serverhive-watch --workflow watch.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" -R greed-d/serverhive-watch
```

On the first run after adding the Telegram secrets you should see:

- a low-priority **ServerHive watcher started** push;
- a low-priority **ServerHive group watch active** push;
- `group: baseline ...` in the log, not `group: skipped`.

The group check baselines the latest message. If @Shra1V32 posted something in
the previous 6 hours, it is included in the first alert.

### Local test without sending anything

```bash
python watcher.py --dry-run --state /tmp/serverhive-state.json
```

Dry-run prints notifications to stdout and does not save state. To test the
notification path itself, export `NTFY_TOPIC` and run:

```bash
python watcher.py --test-notify
```

## Schedule and latency

- GitHub's minimum scheduled interval is 5 minutes, and runs are often delayed
  by 5–20 minutes (occasionally longer under load). This is near-real-time, not
  instant.
- For near-instant alerts, run the same script on an always-on VPS with
  `--loop 60`; see `deploy/`.
- GitHub disables scheduled workflows in public repositories after 60 days of
  repository inactivity. `.github/workflows/keepalive.yml` makes a small monthly
  commit to prevent that.
- A public repository gets free standard-runner minutes. A private repository
  with 5-minute checks would exceed the 2,000 free minutes/month.

## Security notes

- The Telegram session string is equivalent to being logged in. GitHub Actions
  secrets are not exposed to public-repo visitors or fork PRs, but they do live
  in GitHub's infrastructure. If that trust model is not acceptable, run the
  watcher on your own VPS instead.
- The public workflow logs contain message IDs and classifications only. Message
  text is sent to ntfy, not printed in the log.
- The ntfy topic is the only credential for reading alerts. Keep it private and
  rotate it if it leaks.
- Reusing a Telegram session from GitHub's IP ranges can, in rare cases, trigger
  Telegram's suspicious-activity checks. If that happens, move to a VPS.

## Limitations

- `giveaway.serverhive.in` is behind Cloudflare and currently returns `502`.
  That is the expected baseline; the useful event is the transition to a
  reachable response. If Cloudflare serves a challenge page, the alert says so
  and you may need to open it manually.
- The Telegram account must be a member of `@ServerRentals`. If it is not, the
  group check alerts with an error and the public checks keep running.
- The state cache is best-effort. A lost cache can cause duplicate alerts. On a
  fresh baseline the watcher still flags recent channel giveaways (48 hours),
  recent @Shra1V32 messages (6 hours), and a live site, but a giveaway older
  than those windows can be missed.
- Edits to old messages are not treated as new messages.

## Troubleshooting

| Log line | Meaning / fix |
| --- | --- |
| `group: skipped` | One of `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING` is missing. |
| `group: session not authorized` | Session was revoked. Run `login.py` again and update the secret. |
| `group: history fetch failed` | The account is not in the group or the group restricts reading. Join `@ServerRentals` with that account. |
| `Cannot resolve @ServerRentals` | Wrong `TG_GROUP`, or the account cannot see the group. |
| `ntfy: HTTP error 403` | Check the topic name and that it is not reserved by another account. |
| Workflow does not run | Check the Actions tab; scheduled workflows can be disabled for inactivity or by repository settings. |
| Site is always `502` | Expected while no giveaway is deployed. |

Optional environment overrides (set as repository variables or in the VPS env
file):

- `TG_GROUP` (default `ServerRentals`)
- `TG_SENDER` (default `Shra1V32`)
- `NTFY_SERVER` (default `https://ntfy.sh`)
- `STATE_FILE` (default `.state.json`)

## Repository layout

```
watcher.py                     main loop / single-shot watcher
login.py                       one-time Telethon StringSession generator
tests/                         parser and dedupe tests (stdlib unittest)
.github/workflows/watch.yml    the 5-minute watcher
.github/workflows/keepalive.yml monthly repo-activity commit
deploy/                        optional systemd/VPS setup
```

## License

MIT
