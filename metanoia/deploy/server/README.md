# Web service unit (Oracle Cloud box)

`metanoia.service` is the copy of record for the unit that serves the
site on port 80. The live file is `/etc/systemd/system/metanoia.service`
— this directory exists so a change to it survives a rebuild of the machine.

Install / update:

    sudo cp deploy/server/metanoia.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now metanoia

There is **no nginx**. gunicorn runs the ASGI app directly on `0.0.0.0:80` with
`CAP_NET_BIND_SERVICE`, and whitenoise serves `staticfiles/` from inside the app
process, so this unit plus postgresql and redis-server is the whole stack.

Check it:

    systemctl status metanoia
    sudo journalctl -u metanoia -f
    python3 run.py --check          # verifies postgres, redis, migrations, static

Two details in the unit are deliberate and easy to undo by accident:

* **`Wants=`, not `Requires=`**, on postgresql/redis-server. `Requires=` sounds
  stricter and is worse: `systemctl stop redis-server` propagates into stopping
  the site, and it does *not* come back when redis returns. Start ordering is
  already guaranteed by `After=`.
* **`Restart=always`**, so a worker that dies while redis is briefly away is
  retried until it can start again.

## Uploaded files (`/media/*`) must reach Django

Caddy proxies every path, `/media/*` included, to gunicorn. That is what keeps
paid course files private: `core.media_access` checks each request against the
viewer's enrolment / payment (`apps.learning.content_files`) before streaming the
file. The installed `/etc/caddy/Caddyfile` must therefore contain **no**
`file_server`, `root` or `handle /media/*` / `handle_path /media/*` block that
reads `metanoia/media/` from disk. If one exists, delete that block so
the site block is just `encode` + `reverse_proxy` (as in `./Caddyfile`), then
`sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
`SERVE_MEDIA_FROM_DJANGO` must stay unset or `true` in the app's environment.
