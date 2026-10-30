# Port 80 redirect install (owner request 2026-10-15)

`http://<name>.local/` answers with a redirect to `http://<name>.local:5000/` (path and query kept).
The panel itself stays on :5000. ship.py does not install units, so this is a one-time install:

```
sudo install -o root -g root -m 0755 deploy/matrix_port80_redirect.py /usr/local/libexec/matrix-port80-redirect.py
sudo install -o root -g root -m 0644 deploy/matrix-port80.service /etc/systemd/system/matrix-port80.service
sudo systemctl daemon-reload && sudo systemctl enable --now matrix-port80.service
curl -sI http://127.0.0.1/pair | grep -i '^location'   # -> http://127.0.0.1:5000/pair
```

Rollback: `sudo systemctl disable --now matrix-port80.service && sudo rm /etc/systemd/system/matrix-port80.service /usr/local/libexec/matrix-port80-redirect.py && sudo systemctl daemon-reload`.

Preflight (port 80 must be free): `ss -ltn 'sport = :80'` prints only the header line.
After a reboot: `systemctl is-active matrix-port80` and the curl check above.

First-run setup portal: it binds 10.42.0.1:80 while the setup hotspot is up. The redirect checks
every second and closes its listener while 10.42.0.1 is a local address, so the portal can always
bind. If the portal ever loses a race it exits and matrix-network-setup restarts it 5 s later, when
the listener is already closed. Manual recovery if setup ever loops: `sudo systemctl stop matrix-port80`.

Limits: 16 connections at once (more are dropped), 3 s per connection, no journal output.
Paths over 2 KB or with spaces/non-ASCII redirect to `/` on :5000.
