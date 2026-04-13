# IMAGIAX Monitoring System

Bash-based health monitoring with Telegram alerts. Runs every 5 minutes via cron. Checks containers, HTTP endpoints, disk and RAM. Alerts only on state changes (no spam).

## What It Monitors

| Check | Threshold | Alert When |
|-------|-----------|------------|
| Container status | All 5 must be `running` | Container stops or becomes `unhealthy` |
| `/api/salud` | HTTP 200 | Non-200 response or timeout |
| `/health/db` | HTTP 200 | Non-200 response or timeout |
| OHIF `/` | HTTP 200 (follows redirects) | Non-200 or timeout |
| Disk usage | < 80% | Usage ≥ 80% |
| RAM available | > 200MB | Available ≤ 200MB |

## Telegram Setup

### Step 1 — Create a Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. `IMAGIAX Monitor`)
4. Choose a username (must end in `bot`, e.g. `imagiax_monitor_bot`)
5. BotFather gives you a token like: `7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
6. **Save this token** — this is your `BOT_TOKEN`

### Step 2 — Get Your Chat ID

1. Start a conversation with your new bot (search its username, press Start)
2. Send any message to the bot (e.g. `/start`)
3. Fetch the chat_id by visiting in your browser:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
4. Look for `"chat":{"id":XXXXXXX}` in the JSON response
5. **Save this number** — this is your `CHAT_ID`

   For group notifications: add the bot to a group, send a message mentioning it,
   then use the same `/getUpdates` URL. The group's chat_id will be negative (e.g. `-1001234567890`).

### Step 3 — Configure on Server

SSH into the server and edit the config:
```bash
ssh -i ~/.ssh/do_imagiax root@imagiax.dev
nano /root/monitoring/.telegram_config
```

Replace the placeholders:
```bash
BOT_TOKEN=7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CHAT_ID=123456789
```

Set restrictive permissions:
```bash
chmod 600 /root/monitoring/.telegram_config
```

### Step 4 — Test

Run the script manually and watch for a Telegram message:
```bash
/root/monitoring/health_check.sh
```

Check the log:
```bash
tail -20 /root/backups/logs/health.log
```

## File Locations (on server)

```
/root/monitoring/
├── health_check.sh        # Main monitoring script (chmod 750)
├── .telegram_config       # Credentials — never share! (chmod 600)
└── state/                 # State files for deduplication
    ├── container_fastapi-imagiax
    ├── container_nginx-imagiax
    ├── container_ohif-imagiax
    ├── container_orthanc-imagiax
    ├── container_postgres-imagiax
    ├── endpoint_api_salud
    ├── endpoint_health_db
    ├── endpoint_ohif_root
    ├── resource_disk
    └── resource_ram

/root/backups/logs/
└── health.log             # All check runs logged here
```

## Cron Schedule

The script runs every 5 minutes under `flock` to prevent concurrent execution:
```
*/5 * * * * /usr/bin/flock -n /tmp/.health_check.lock /root/monitoring/health_check.sh >> /root/backups/logs/health.log 2>&1
```

View the cron entry: `crontab -l`

## Alert Format

**DOWN alert:**
```
🔴 DOWN — imagiax.dev
container_fastapi-imagiax
Container status: exited
Time: 2026-04-04T15:30:00Z
```

**RECOVERED alert:**
```
🟢 RECOVERED — imagiax.dev
container_fastapi-imagiax (down for ~10m)
Container running normally
Time: 2026-04-04T15:40:00Z
```

## Rollback

To disable monitoring:
```bash
crontab -e  # delete the health_check.sh line
```

The Docker healthchecks added to compose are harmless and can remain.

## Manual Testing

**Force a state transition alert:**
```bash
# Delete state file to simulate first run after recovery
rm /root/monitoring/state/container_fastapi-imagiax
# Then stop a container
docker stop fastapi-imagiax
# Run the script manually — should send DOWN alert
/root/monitoring/health_check.sh
# Start the container back
docker start fastapi-imagiax
# Wait ~25s for FastAPI to be healthy, then run again
/root/monitoring/health_check.sh
# Should send RECOVERED alert
```
