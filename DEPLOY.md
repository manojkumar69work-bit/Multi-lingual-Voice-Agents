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

`/api/health` returns `"ok": true` once TTS, LiveKit and the database answer and
the agent worker has sent a heartbeat. The admin console shows the same chips.

Data lives in `./data` (`calls.db`, `recordings/`). The first start imports a
`tenants.json` if one is present in the image and the table is empty.

## Backups

`scripts/backup.sh` copies the database with sqlite's online backup and syncs
recordings, keeping 30 days:

```
15 2 * * *  cd /opt/voice-agent && scripts/backup.sh ./data ./backups >> backups/backup.log 2>&1
```

## Updating

```
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

Client config is in the database, so an update never touches it.

## Phone numbers

See `PHONE_SETUP.md`. With a trunk pointed at this LiveKit, each client's
**Phone number** field routes their calls; nothing else changes.
