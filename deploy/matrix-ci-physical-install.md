# Physical CI verifier appliance installation

Install these assets once from a separately reviewed release on the appliance. The CI job never uploads or replaces verifier bytes. Keep the release tree root-owned and non-group/world-writable while running this procedure.

## Fixed verifier and PID-1 admission

```sh
set -eu

# Freeze expected digests from the separately reviewed release input before any
# installation write. Never compute these expected digests from the installed destinations:
# live files cannot self-certify that they are the reviewed bytes.
MATRIX_CI_PHYSICAL_VERIFIER_SHA256=$(sha256sum deploy/matrix_ci_physical_verifier.py | cut -d' ' -f1)
MATRIX_CI_PHYSICAL_DISPATCHER_SHA256=$(sha256sum deploy/matrix_ci_physical_dispatcher.py | cut -d' ' -f1)
MATRIX_CI_INSTALL_RGB_BINDINGS_SHA256=$(sha256sum deploy/matrix_ci_install_rgb_bindings.py | cut -d' ' -f1)
VERIFY_MATRIX_CI_PHYSICAL_INSTALL_SHA256=$(sha256sum deploy/verify_matrix_ci_physical_install.py | cut -d' ' -f1)
MATRIX_DEVICE_RESTART_BROKER_SHA256=$(sha256sum deploy/matrix_device_restart_broker.py | cut -d' ' -f1)
MATRIX_CI_PHYSICAL_SERVICE_SHA256=$(sha256sum deploy/matrix-ci-physical.service | cut -d' ' -f1)
MATRIX_CI_PHYSICAL_PATH_SHA256=$(sha256sum deploy/matrix-ci-physical.path | cut -d' ' -f1)
MATRIX_DEVICE_RESTART_SERVICE_SHA256=$(sha256sum deploy/matrix-device-restart.service | cut -d' ' -f1)
MATRIX_DEVICE_RESTART_PATH_SHA256=$(sha256sum deploy/matrix-device-restart.path | cut -d' ' -f1)
MATRIX_WEB_SERVICE_SHA256=$(sha256sum deploy/matrix-web.service | cut -d' ' -f1)
MATRIX_CI_PHYSICAL_CONF_SHA256=$(sha256sum deploy/matrix-ci-physical.conf | cut -d' ' -f1)
MATRIX_DISPLAY_CONF_SHA256=$(sha256sum deploy/matrix-display.conf | cut -d' ' -f1)
MATRIX_DEVICE_RESTART_CONF_SHA256=$(sha256sum deploy/matrix-device-restart.conf | cut -d' ' -f1)

install -o root -g root -m 0755 deploy/matrix_ci_physical_verifier.py /usr/local/libexec/matrix-ci-physical-verifier
install -o root -g root -m 0755 deploy/matrix_ci_physical_dispatcher.py /usr/local/libexec/matrix-ci-physical-dispatcher
install -o root -g root -m 0755 deploy/matrix_ci_install_rgb_bindings.py /usr/local/libexec/matrix-ci-install-rgb-bindings
install -o root -g root -m 0755 deploy/verify_matrix_ci_physical_install.py /usr/local/libexec/verify-matrix-ci-physical-install
install -o root -g root -m 0755 deploy/matrix_device_restart_broker.py /usr/local/libexec/matrix-device-restart-broker
install -o root -g root -m 0644 deploy/matrix-ci-physical.service /etc/systemd/system/matrix-ci-physical.service
install -o root -g root -m 0644 deploy/matrix-ci-physical.path /etc/systemd/system/matrix-ci-physical.path
install -o root -g root -m 0644 deploy/matrix-device-restart.service /etc/systemd/system/matrix-device-restart.service
install -o root -g root -m 0644 deploy/matrix-device-restart.path /etc/systemd/system/matrix-device-restart.path
install -o root -g root -m 0644 deploy/matrix-web.service /etc/systemd/system/matrix-web.service
install -o root -g root -m 0644 deploy/matrix-ci-physical.conf /etc/tmpfiles.d/matrix-ci-physical.conf
install -o root -g root -m 0644 deploy/matrix-display.conf /etc/tmpfiles.d/matrix-display.conf
install -o root -g root -m 0644 deploy/matrix-device-restart.conf /etc/tmpfiles.d/matrix-device-restart.conf

# Fail if installed bytes or metadata differ from the reviewed release.
for pair in \
  deploy/matrix_ci_physical_verifier.py:/usr/local/libexec/matrix-ci-physical-verifier \
  deploy/matrix_ci_physical_dispatcher.py:/usr/local/libexec/matrix-ci-physical-dispatcher \
  deploy/matrix_ci_install_rgb_bindings.py:/usr/local/libexec/matrix-ci-install-rgb-bindings \
  deploy/verify_matrix_ci_physical_install.py:/usr/local/libexec/verify-matrix-ci-physical-install \
  deploy/matrix_device_restart_broker.py:/usr/local/libexec/matrix-device-restart-broker \
  deploy/matrix-ci-physical.service:/etc/systemd/system/matrix-ci-physical.service \
  deploy/matrix-ci-physical.path:/etc/systemd/system/matrix-ci-physical.path \
  deploy/matrix-device-restart.service:/etc/systemd/system/matrix-device-restart.service \
  deploy/matrix-device-restart.path:/etc/systemd/system/matrix-device-restart.path \
  deploy/matrix-web.service:/etc/systemd/system/matrix-web.service \
  deploy/matrix-ci-physical.conf:/etc/tmpfiles.d/matrix-ci-physical.conf \
  deploy/matrix-display.conf:/etc/tmpfiles.d/matrix-display.conf \
  deploy/matrix-device-restart.conf:/etc/tmpfiles.d/matrix-device-restart.conf
do
  test "$(sha256sum "${pair%%:*}" | cut -d' ' -f1)" = "$(sha256sum "${pair#*:}" | cut -d' ' -f1)"
done

# Create and normalize the host-owned parent and canonical lock before either
# service is enabled or started. An existing canonical inode is not replaced.
/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/matrix-ci-physical.conf
/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/matrix-display.conf
/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/matrix-device-restart.conf
test "$(stat -c '%U:%G:%a:%F' /run/matrix-display)" = "root:root:755:directory"
test -f /run/matrix-display/matrix-display.lock
test ! -L /run/matrix-display/matrix-display.lock
test "$(stat -c '%U:%G:%a:%h' /run/matrix-display/matrix-display.lock)" = "root:root:644:1"
test "$(stat -c '%U:%G:%a:%F' /run/matrix-device-restart)" = "root:root:700:directory"
test "$(stat -c '%U:%G:%a:%F' /run/matrix-device-restart/state)" = "root:root:700:directory"
test -f /run/matrix-device-restart/state/transaction.lock
test ! -L /run/matrix-device-restart/state/transaction.lock
test "$(stat -c '%U:%G:%a:%h' /run/matrix-device-restart/state/transaction.lock)" = "root:root:644:1"
test "$(stat -c '%U:%G:%a:%F' /run/matrix-device-restart/submit)" = "root:matrix-web:1730:directory"
restart_root_device="$(stat -c '%d' /run/matrix-device-restart)"
test "$(stat -c '%d' /run/matrix-device-restart/state)" = "$restart_root_device"
test "$(stat -c '%d' /run/matrix-device-restart/submit)" = "$restart_root_device"
unset restart_root_device
```

The whole-device restart route runs inside the hardware-owning web service, which
must retain effective uid 0 because the pinned RGB Matrix native library rejects
non-root initialization even with `CAP_SYS_RAWIO`. The unit is nevertheless
confined with `NoNewPrivileges`, an exact capability/device allow-list, a strict
read/write namespace, an empty private home view with only the application tree
bound read-only, zero-byte read-only overlays for `/etc/shadow` and
`/etc/gshadow`, an empty read-only `/etc/ssh` filesystem, and inaccessible
system-manager private and DBus sockets. The route can only create the fixed zero-byte
`/run/matrix-device-restart/submit/request` marker; it cannot choose a command,
unit, delay, or alternate path. The separately installed root-owned broker
validates and atomically moves that marker into the inaccessible root-only
`/run/matrix-device-restart/state` namespace while holding the fixed transaction
lock, waits its fixed 30-second response margin, and invokes only the fixed reboot
operation. Duplicate submissions coalesce. The submission namespace is a
root-owned `1730` sticky directory in group `matrix-web`; the state namespace is
root-only mode `0700`, and its immutable transaction inode is a root-owned `0644`
single-link regular file. Both children reside beneath one broker-writable
`/run/matrix-device-restart` mount, so the atomic no-replace claim cannot cross a
systemd writable-path bind-mount boundary. Only the submission child is writable
by the web unit, and the state child is inaccessible there. The web service cannot erase an active request or a
forensic terminal marker. No sudo, setuid helper, or optional dependency is used.

### Durable restart terminal states and manual recovery

The broker preserves a root-claimed, zero-byte `root:root` mode-`0600` forensic
terminal marker instead of retrying after an uncertain or unsafe outcome. The
active `request` is likewise root-claimed after broker admission:

- `request.failed` means the fixed reboot command failed or timed out.
- `request.cancelled` means the broker was interrupted before completing it.
- `request.invalid` means admission metadata or marker content failed validation.

Restart submissions are consumed without reboot while any one of these markers
exists. The API confirms only durable publication in the isolated submission
namespace; it cannot inspect, identify, or remove the inaccessible forensic state.
Operators must therefore investigate the broker journal
(`journalctl -u matrix-device-restart.service`), record the marker metadata and
inode, and determine why the reboot failed, was cancelled, or was rejected before
accepting a later restart attempt. Preserve any required forensic evidence outside
this runtime namespace through the approved incident process.

After that investigation, an administrator may clear exactly one diagnosed
terminal marker with the following descriptor-relative procedure. Set `MARKER`
to one of the three literal names shown; never use a glob or recursively remove
the runtime directory.

```sh
MARKER=request.failed /usr/bin/python3 -I -B <<'PY'
import fcntl
import os
import stat

marker = os.environ["MARKER"]
if marker not in {"request.failed", "request.cancelled", "request.invalid"}:
    raise SystemExit("refusing unknown restart terminal marker")
directory_fd = os.open(
    "/run/matrix-device-restart/state",
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
)
lock_fd = None
marker_fd = None
try:
    directory = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != 0
        or directory.st_gid != 0
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise RuntimeError("unsafe restart runtime directory")
    lock_before = os.stat(
        "transaction.lock", dir_fd=directory_fd, follow_symlinks=False,
    )
    lock_fd = os.open(
        "transaction.lock",
        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory_fd,
    )
    lock_opened = os.fstat(lock_fd)
    if (
        not stat.S_ISREG(lock_opened.st_mode)
        or (lock_opened.st_uid, lock_opened.st_gid) != (0, 0)
        or stat.S_IMODE(lock_opened.st_mode) != 0o644
        or lock_opened.st_nlink != 1
        or (lock_before.st_dev, lock_before.st_ino)
        != (lock_opened.st_dev, lock_opened.st_ino)
    ):
        raise RuntimeError("unsafe restart transaction lock")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    lock_current = os.stat(
        "transaction.lock", dir_fd=directory_fd, follow_symlinks=False,
    )
    if (
        (lock_opened.st_dev, lock_opened.st_ino)
        != (lock_current.st_dev, lock_current.st_ino)
        or not stat.S_ISREG(lock_current.st_mode)
        or (lock_current.st_uid, lock_current.st_gid) != (0, 0)
        or stat.S_IMODE(lock_current.st_mode) != 0o644
        or lock_current.st_nlink != 1
    ):
        raise RuntimeError("restart transaction lock changed during recovery")
    before = os.stat(marker, dir_fd=directory_fd, follow_symlinks=False)
    marker_fd = os.open(
        marker,
        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory_fd,
    )
    opened = os.fstat(marker_fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_uid, opened.st_gid) != (0, 0)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink != 1
        or opened.st_size != 0
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise RuntimeError("unsafe restart terminal marker")
    current = os.stat(marker, dir_fd=directory_fd, follow_symlinks=False)
    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError("restart terminal marker changed during validation")
    os.unlink(marker, dir_fd=directory_fd)
    os.fsync(directory_fd)
finally:
    if marker_fd is not None:
        os.close(marker_fd)
    if lock_fd is not None:
        os.close(lock_fd)
    os.close(directory_fd)
PY
```

Run the endpoint again only after the diagnosed marker has been safely cleared.

`claim` is the single durable admission transaction: the dispatcher writes through a unique, descriptor-confined temporary inode, loops short writes, fsyncs it, and atomically publishes it with `renameat2(RENAME_NOREPLACE)`. Interrupted empty/partial unique temporaries are never reused or deleted by a later writer and therefore cannot wedge admission or steal a live writer. Reports use the same unique-temporary publication discipline. There is no dispatcher `systemctl start` and no shared start/flock lock, so a queued path activation survives SSH loss without deadlocking the `Type=exec` verifier. `RuntimeMaxSec` bounds the live verifier; `ExecStopPost` restores production after timeout, signal, or hard kill.

The reviewed tmpfiles policy creates a root-owned `0755` parent and canonical root-owned `0644` single-link lock before either service starts. The tmpfiles `f` line creates the file only when absent and does not replace an existing inode. Both services receive `/run/matrix-display` through a read-only systemd namespace and open the canonical inode read-only for `flock`; neither service truncates, rewrites, unlinks, or replaces it. Both implementations descriptor-walk the root-owned, non-writable `/run` ancestry and revalidate parent and inode identity after acquiring the lock. Their `UMask=0077` therefore cannot narrow the host-created lock's canonical `0644` mode.

## Digest-pinned RGB binding provenance

Do **not** generate trust from the live admin venv during appliance installation. From the separately reviewed RGB Matrix build artifact, review the exact three files and generate a release-side manifest:

```sh
cd REVIEWED_RGB_ARTIFACT_ROOT
printf '%s  %s\n' \
  "$(sha256sum rgbmatrix/__init__.py | cut -d' ' -f1)" rgbmatrix/__init__.py \
  "$(sha256sum rgbmatrix/core.so | cut -d' ' -f1)" rgbmatrix/core.so \
  "$(sha256sum rgbmatrix/graphics.so | cut -d' ' -f1)" rgbmatrix/graphics.so \
  > rgbmatrix.sha256
# Record the artifact release/version, builder identity, source revision, native
# dependency provenance, and this manifest SHA-256 in the immutable review.
sha256sum rgbmatrix.sha256
```

Transfer that reviewed artifact and manifest through the administrator release channel, never through candidate CI. The installer descriptor-walks both source and destination ancestry with no-follow checks, requires root-owned non-writable regular single-link source files, rejects extra entries, and verifies the independent manifest before writing. It builds and fsyncs the complete exact tree in a process-owned unique sibling staging directory, then performs atomic no-replace publication of the final destination. The final name therefore never exposes partial trusted content; ordinary pre-publication failures are cleaned up for a safe retry. A hostile existing final destination or prepositioned staging sibling is left untouched and fails closed or is skipped rather than removed.

```sh
# Destination must not already exist; replacement requires a new immutable review.
test ! -e /usr/local/lib/matrix-ci-python
/usr/bin/python3 -I -B /usr/local/libexec/matrix-ci-install-rgb-bindings \
  --source /root/reviewed-rgb-release/rgbmatrix \
  --manifest /root/reviewed-rgb-release/rgbmatrix.sha256 \
  --destination /usr/local/lib/matrix-ci-python
/usr/bin/python3 -I -B -c 'import sys; sys.path.insert(0, "/usr/local/lib/matrix-ci-python"); import rgbmatrix, rgbmatrix.core, rgbmatrix.graphics; print(rgbmatrix.__file__)'
```

The native `.so` dependency set and package build provenance require independent release review; file ownership alone is not provenance.

## Dedicated forced-command key

Create a dedicated Ed25519 key used only by this physical gate. Put exactly one completed line from `matrix-ci-physical-authorized-key.example` in root's `authorized_keys`; do not reuse a deploy, shell, SFTP, SCP, or administrator key. The install gate accepts only a matching `ssh-ed25519` blob with the example's exact, effective options field before the key type (optionally prefixed by one canonical `from="RUNNER_EGRESS_CIDR",` restriction); options placed in the comment, reordered/duplicated/escaped options, and unrestricted copies fail closed.

```sh
install -d -o root -g root -m 0700 /root/.ssh
install -o root -g root -m 0600 reviewed-matrix-physical-authorized_keys /root/.ssh/authorized_keys
test "$(stat -c '%U:%G:%a' /root/.ssh)" = root:root:700
test "$(stat -c '%U:%G:%a' /root/.ssh/authorized_keys)" = root:root:600
# Compare this dedicated fingerprint with the protected CI private key's public half.
ssh-keygen -lf /root/.ssh/authorized_keys
# Verify the dedicated fingerprint appears exactly once and no unrestricted copy exists.
test "$(ssh-keygen -lf /root/.ssh/authorized_keys | grep -Fc 'EXPECTED_DEDICATED_FINGERPRINT')" -eq 1
/usr/sbin/sshd -t
/usr/sbin/sshd -T -C 'user=root,host=RUNNER_RESOLVED_HOSTNAME,addr=RUNNER_EGRESS_IP,laddr=APPLIANCE_SSH_ADDRESS,lport=APPLIANCE_SSH_PORT,rdomain=SSH_ROUTING_DOMAIN' | grep -E '^(authorizedkeysfile|authorizedkeyscommand|authorizedkeyscommanduser|permitrootlogin) '
```

The install gate structurally parses the complete effective output and requires exactly one key source resolving to `/root/.ssh/authorized_keys`, `AuthorizedKeysCommand none`, a canonical `AuthorizedKeysCommandUser`, and `PermitRootLogin forced-commands-only`; alternate/multiple paths, percent tokens, duplicate directives, and malformed output fail closed. Where runner egress is stable, prepend `from="RUNNER_EGRESS_CIDR"` to the authorized-key options. Independently attempt shell, PTY, forwarding, SCP, and SFTP with this dedicated key and require rejection; only the exact `verify <40-lowercase-hex-sha> <decimal-pipeline-id>` command may reach the dispatcher.

## Static and install-time gates

```sh
systemctl daemon-reload
/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/matrix-ci-physical.conf
/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/matrix-display.conf
systemd-analyze verify /etc/systemd/system/matrix-ci-physical.service /etc/systemd/system/matrix-ci-physical.path /etc/systemd/system/matrix-device-restart.service /etc/systemd/system/matrix-device-restart.path /etc/systemd/system/matrix-web.service
systemctl enable --now matrix-ci-physical.path matrix-device-restart.path
systemctl is-enabled --quiet matrix-ci-physical.path
systemctl show matrix-ci-physical.service -p Type -p RuntimeMaxUSec -p CapabilityBoundingSet -p ExecStart -p ExecStop -p ExecStopPost -p Requires -p After -p ReadOnlyPaths -p UMask
systemctl show matrix-web.service -p InaccessiblePaths -p CapabilityBoundingSet -p Requires -p After -p ReadOnlyPaths -p UMask
systemctl cat matrix-ci-physical.service matrix-ci-physical.path matrix-device-restart.service matrix-device-restart.path matrix-web.service
/usr/bin/python3 -I -B /usr/local/libexec/verify-matrix-ci-physical-install \
  --dedicated-key-fingerprint SHA256:REVIEWED_DEDICATED_KEY_FINGERPRINT \
  --runner-address RUNNER_EGRESS_IP \
  --runner-hostname RUNNER_RESOLVED_HOSTNAME \
  --local-address APPLIANCE_SSH_ADDRESS \
  --local-port APPLIANCE_SSH_PORT \
  --routing-domain SSH_ROUTING_DOMAIN \
  --expected-artifact-sha256 matrix-ci-physical-verifier="$MATRIX_CI_PHYSICAL_VERIFIER_SHA256" \
  --expected-artifact-sha256 matrix-ci-physical-dispatcher="$MATRIX_CI_PHYSICAL_DISPATCHER_SHA256" \
  --expected-artifact-sha256 matrix-ci-install-rgb-bindings="$MATRIX_CI_INSTALL_RGB_BINDINGS_SHA256" \
  --expected-artifact-sha256 verify-matrix-ci-physical-install="$VERIFY_MATRIX_CI_PHYSICAL_INSTALL_SHA256" \
  --expected-artifact-sha256 matrix-device-restart-broker="$MATRIX_DEVICE_RESTART_BROKER_SHA256" \
  --expected-artifact-sha256 matrix-ci-physical.service="$MATRIX_CI_PHYSICAL_SERVICE_SHA256" \
  --expected-artifact-sha256 matrix-ci-physical.path="$MATRIX_CI_PHYSICAL_PATH_SHA256" \
  --expected-artifact-sha256 matrix-device-restart.service="$MATRIX_DEVICE_RESTART_SERVICE_SHA256" \
  --expected-artifact-sha256 matrix-device-restart.path="$MATRIX_DEVICE_RESTART_PATH_SHA256" \
  --expected-artifact-sha256 matrix-web.service="$MATRIX_WEB_SERVICE_SHA256" \
  --expected-artifact-sha256 matrix-ci-physical.conf="$MATRIX_CI_PHYSICAL_CONF_SHA256" \
  --expected-artifact-sha256 matrix-display.conf="$MATRIX_DISPLAY_CONF_SHA256" \
  --expected-artifact-sha256 matrix-device-restart.conf="$MATRIX_DEVICE_RESTART_CONF_SHA256"
```

Every SSH tuple value above must come from the real accepted connection: source address, sshd-resolved source hostname, appliance local address and port, and routing domain. The gate passes the complete tuple to `sshd -T -C`, because any of `Match Host`, `Match Address`, `Match LocalAddress`, `Match LocalPort`, or `Match RDomain` can change the effective key policy. These commands are installation instructions only; repository verification does not claim that this live appliance gate was run.

Before enabling protected CI, run the appliance-gated lifecycle acceptance that uses harmless substitute units and a temporary lock (never `matrix-web` or hardware): queued activation, dispatcher/SSH loss before and after activation, report-publication race, normal stop, runtime timeout, SIGKILL, exact-once verifier admission, bounded recovery, and a competing process failing to acquire the same canonical lock inode. If native construction raises before returning an object, a fresh driver blackout is explicitly best effort; a fake that succeeds on its second construction does not prove GPIO transfer or real-panel blackout. When the driver was returned but canvas creation fails, the verifier independently attempts brightness 1, driver clear, an obtainable canvas clear/swap route, and exposed close hooks while preserving the primary exception. Then perform separately approved physical acceptance. Repository/static tests do not prove installed PID-1 behavior, effective sshd policy, or hardware recovery.

## Protected GitLab variables

Configure:

- `MATRIX_DEVICE_TARGET`: a protected variable containing the fixed SSH target.
- `MATRIX_DEVICE_SSH_KEY`: a protected FILE variable containing only the dedicated private key.
- `MATRIX_DEVICE_KNOWN_HOSTS`: a protected FILE variable containing the pinned appliance host key.

Restrict every variable to the protected production environment/ref scope. They are never available to merge-request or branch jobs; the CI rule permits only a protected default-branch push. Independently inspect the live GitLab variable type, protection flag, environment scope, target protection, and non-exposure to unprotected refs before enabling the job. Do not print values or store them in repository files or artifacts.
