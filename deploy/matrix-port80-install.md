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

Note: the first-run setup portal binds 10.42.0.1:80 only while the hotspot is up (matrix-network-setup.service,
ordered before this unit). If port 80 is busy the unit retries every 3 s; the panel on :5000 is unaffected.
