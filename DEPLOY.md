# Deploying to a server

One Linux host with Docker is enough for tens of clients. Everything runs from
`docker-compose.prod.yml`: the API and portals, the TTS service, the agent
worker, LiveKit, Redis and Caddy (HTTPS).

## Before the first start

1. Point your domain's A record at the server.
2. Open ports **80, 443** (TCP), **7881** (TCP) and **50000–50100** (UDP) in the
   firewall. The UDP range carries call audio.
3. `cp .env.production.example .env` and fill every value. `APP_ENV=production`
   is set by the compose file, so the server **refuses to start** with a dev
   session secret or the default admin password.
4. Put the admin password in as a hash, not plain text:
   ```
   docker run --rm -it voice-agent:latest python -c "import passwords; print(passwords.hash_password('your-password'))"
   ```

## Start

```
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
curl -s https://$DOMAIN/api/health
```

`/api/health` returns `"ok": true` once TTS, LiveKit and the database answer.
The agent worker's heartbeat is reported separately (`worker_seen_at`); the
admin console shows all of it as chips. Place one test call before handing
the link to a client.

Data lives in `./data` (`calls.db`, `recordings/`). In production the demo
`tenants.json` is never imported; create clients in the admin console.

## Backups

`scripts/backup.sh` copies the database with sqlite's online backup and syncs
recordings, keeping 30 days:

```
15 2 * * *  cd /opt/voice-agent && mkdir -p backups && scripts/backup.sh ./data ./backups >> backups/backup.log 2>&1
```

The host needs `sqlite3` and `rsync` (`apt-get install sqlite3 rsync`). Run the
cron as root: the data files are created by the containers.

## Updating

```
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

Client config is in the database, so an update never touches it.

## Phone numbers

The stack includes LiveKit's SIP bridge (`sip` service, port 5060/udp and
10000–10100/udp). Point the carrier trunk at `sip:5060` on this host, create
the inbound trunk and dispatch rule per `PHONE_SETUP.md`, then put each
client's number in their **Phone number** field. Demo clients from
`tenants.json` are never imported in production; set `DEMO_CLIENT_ID` to a
client you created if the landing page's demo button should work.
