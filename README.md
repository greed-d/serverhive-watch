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

The backup's first successful run sends a "watcher started" notification and
baselines the current channel/site state, so old posts are not replayed. A giveaway post
from the previous 48 hours, a recent message from @Shra1V32 (previous 6 hours),
or a currently-live giveaway page is still flagged on that first run.

**If the giveaway fills in seconds, use the real-time watcher below.** GitHub
Actions has a five-minute minimum and often starts late; it cannot react in
time. `live_watch.py` runs on an always-on host and pushes an alert in about a
second.

## How it runs

There are two modes:

- **Real-time (`live_watch.py`)** — the recommended mode for a fast giveaway.
  It stays connected to Telegram with a user session, reacts to new
  `@ServerHive` and `@ServerRentals` messages through Telethon event handlers,
  and polls the giveaway page every few seconds. It must run on a VPS, a
  Raspberry Pi, or an always-on PC.
- **Backup (`watcher.py`)** — a single-shot program run by
  `.github/workflows/watch.yml` every 5 minutes. It caches `.state.json` between
  runs and is only a slow fallback. The state stores message IDs, timestamps,
  HTTP status, and content hashes — not message text.

If the Telegram secrets are not configured, the backup's group check is skipped
and the public channel/site checks still run. This makes the setup testable
before the Telegram login is finished.

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
only in GitHub Actions secrets and/or the live host's mode-`600` env file. Never
commit it, paste it into an issue, or put it in a shell command with `-b`.

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

## Real-time mode (recommended)

`live_watch.py` is the mode to use for a giveaway that fills within seconds.
It keeps a Telethon user client connected and receives updates as they happen.
A channel post or @Shra1V32 message normally reaches ntfy in about one second.
The giveaway page is also polled every 5 seconds, so a `502 → 2xx` transition
can be detected before the Telegram post.

This is an alerting tool, not an auto-claim bot. Even a one-second alert can
lose a giveaway that fills in two seconds, and automatically submitting a claim
would likely break the giveaway's rules.

### Host

Use a machine that stays online and has a stable network:

- a small VPS (the most reliable option),
- a Raspberry Pi or home server,
- an always-on desktop PC.

GitHub Actions is not suitable: its fastest scheduled interval is five minutes,
and runs are frequently queued for longer. A sleeping laptop also misses alerts.

### Install on Linux

The commands below assume the repository is at `~/Dev/serverhive-watch` and the
service runs as your user. Adjust the paths in the unit file if yours differ.

```bash
cd ~/Dev/serverhive-watch
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

mkdir -p ~/.config
cp deploy/serverhive-watch.env.example ~/.config/serverhive-watch.env
chmod 600 ~/.config/serverhive-watch.env
$EDITOR ~/.config/serverhive-watch.env
```

Fill in at least:

```dotenv
NTFY_TOPIC=your-secret-topic
TG_API_ID=123456
TG_API_HASH=your_api_hash
TG_SESSION_STRING=your_string_session
SITE_POLL_SECONDS=5
```

Generate the session with `python login.py` if you have not already. Keep the
env file at mode `600`; it is equivalent to a logged-in Telegram account.

Install and start the user service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/serverhive-live.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now serverhive-live

# Keep the service running after you log out.
loginctl enable-linger "$USER"

# Follow the live log.
journalctl --user -u serverhive-live -f
```

On startup it sends a low-priority "ServerHive LIVE watcher started" push. A
missing session, an unresolvable chat, or a failed group join produces a
high-priority push instead of failing silently.

`live_watch.py` auto-joins `@ServerHive` and `@ServerRentals` with the logged-in
account (`LIVE_AUTO_JOIN=1`). Set it to `0` only if the account is already a
member of both and you do not want the service to join them.

### Avoid duplicate pushes

If both the real-time service and the 5-minute Actions workflow are active, a
new post can be pushed twice, and the same Telegram session is used from two
hosts at once. Once the live service is confirmed working, disable the Actions
schedule:

1. Open the repository's **Actions** tab.
2. Select **ServerHive giveaway watch**.
3. Use the **⋯** menu to disable the workflow.

You can leave the workflow available for manual `workflow_dispatch` runs. If
you need both modes, create a second Telethon session with `login.py` and use a
different `TG_SESSION_STRING` for the live host.

## Schedule and latency

- `live_watch.py` is the fast path: Telethon events are handled immediately and
  the giveaway page is polled every `SITE_POLL_SECONDS` (default 5, minimum 2).
  Expect roughly one second from Telegram update to ntfy, depending on network
  and ntfy.
- The GitHub Actions workflow is only a backup. GitHub's minimum scheduled
  interval is 5 minutes, and runs are often delayed by 5–20 minutes (occasionally
  longer under load). It cannot provide second-level alerts.
- Disable the Actions schedule after the live service is working to avoid
  duplicate pushes and concurrent use of the same Telegram session.
- GitHub disables scheduled workflows in public repositories after 60 days of
  repository inactivity. `.github/workflows/keepalive.yml` makes a small monthly
  commit to prevent that.
- A public repository gets free standard-runner minutes. A private repository
  with 5-minute checks would exceed the 2,000 free minutes/month.

## Security notes

- The Telegram session string is equivalent to being logged in. GitHub Actions
  secrets are not exposed to public-repo visitors or fork PRs, but they do live
  in GitHub's infrastructure. On a live host, keep it in
  `~/.config/serverhive-watch.env` with mode `600`, not in the repository.
- Running the same session on the live host and in Actions at the same time can
  cause update conflicts or Telegram suspicious-activity checks. Disable the
  Actions schedule after moving to the live watcher, or create a second session.
- If the live host is compromised, the Telegram account and ntfy topic are
  exposed. Use a dedicated small VPS or a machine you control.
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
- The live watcher only alerts messages newer than `LIVE_MAX_AGE_SECONDS`
  (default 600) to avoid replaying stale updates after a reconnect.
- A one-second push does not guarantee a slot in a giveaway that fills in a few
  seconds. This project does not automate claims.

## Troubleshooting

| Log line | Meaning / fix |
| --- | --- |
| `group: skipped` | One of `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING` is missing. |
| `group: session not authorized` | Session was revoked. Run `login.py` again and update the secret. |
| `group: history fetch failed` | The account is not in the group or the group restricts reading. Join `@ServerRentals` with that account. |
| `Cannot resolve @ServerRentals` | Wrong `TG_GROUP`, or the account cannot see the group. |
| `ntfy: HTTP error 403` | Check the topic name and that it is not reserved by another account. |
| Workflow does not run | Check the Actions tab; scheduled workflows can be disabled for inactivity or by repository settings. |
| `live: Telegram session is not authorized` | Session was revoked or copied incorrectly. Run `login.py` again, update `TG_SESSION_STRING` in the live env file, and restart the service. |
| `live: join failed` | The account could not join `@ServerHive`/`@ServerRentals` (permissions, flood wait, or too many chats). Join manually with that account. |
| `live: connect failed` | The host has no network/DNS or Telegram is blocked. Check `journalctl` and `curl -I https://t.me`. |
| `live: site error` | Network error or the giveaway page timed out. It retries every `SITE_POLL_SECONDS`. |
| Service exits immediately | Run `journalctl --user -u serverhive-live -n 100 --no-pager`; usually a missing env value or path. |
| Service stops after a few restarts | `StartLimitBurst` was reached. Fix the config, then `systemctl --user reset-failed serverhive-live` and start it again. |
| Site is always `502` | Expected while no giveaway is deployed. |

Optional environment overrides (set as repository variables or in the VPS env
file):

- `TG_GROUP` (default `ServerRentals`)
- `TG_SENDER` (default `Shra1V32`)
- `NTFY_SERVER` (default `https://ntfy.sh`)
- `SITE_POLL_SECONDS` (live watcher, default `5`, minimum `2`)
- `LIVE_AUTO_JOIN` (live watcher, default `1`)
- `LIVE_MAX_AGE_SECONDS` (live watcher, default `600`)
- `STATE_FILE` (backup watcher, default `.state.json`)

## Repository layout

```
watcher.py                     single-shot / polling watcher (slow backup)
live_watch.py                  event-driven Telethon watcher (real-time)
login.py                       one-time Telethon StringSession generator
tests/                         parser, dedupe, and site-transition tests
.github/workflows/watch.yml    the 5-minute backup watcher
.github/workflows/keepalive.yml monthly repo-activity commit
deploy/                        systemd units and env template
```

## License

MIT
