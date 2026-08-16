# Physical CI verifier appliance installation

Install these assets once from a separately reviewed release on the appliance. The CI job never uploads or replaces verifier bytes. Keep the release tree root-owned and non-group/world-writable while running this procedure.

## Fixed verifier and PID-1 admission

```sh
set -eu
install -o root -g root -m 0755 deploy/matrix_ci_physical_verifier.py /usr/local/libexec/matrix-ci-physical-verifier
install -o root -g root -m 0755 deploy/matrix_ci_physical_dispatcher.py /usr/local/libexec/matrix-ci-physical-dispatcher
install -o root -g root -m 0755 deploy/matrix_ci_install_rgb_bindings.py /usr/local/libexec/matrix-ci-install-rgb-bindings
install -o root -g root -m 0755 deploy/verify_matrix_ci_physical_install.py /usr/local/libexec/verify-matrix-ci-physical-install
install -o root -g root -m 0644 deploy/matrix-ci-physical.service /etc/systemd/system/matrix-ci-physical.service
install -o root -g root -m 0644 deploy/matrix-ci-physical.path /etc/systemd/system/matrix-ci-physical.path
install -o root -g root -m 0644 deploy/matrix-web.service /etc/systemd/system/matrix-web.service
install -o root -g root -m 0644 deploy/matrix-display.conf /etc/tmpfiles.d/matrix-display.conf

# Fail if installed bytes or metadata differ from the reviewed release.
for pair in \
  deploy/matrix_ci_physical_verifier.py:/usr/local/libexec/matrix-ci-physical-verifier \
  deploy/matrix_ci_physical_dispatcher.py:/usr/local/libexec/matrix-ci-physical-dispatcher \
  deploy/matrix_ci_install_rgb_bindings.py:/usr/local/libexec/matrix-ci-install-rgb-bindings \
  deploy/verify_matrix_ci_physical_install.py:/usr/local/libexec/verify-matrix-ci-physical-install \
  deploy/matrix-ci-physical.service:/etc/systemd/system/matrix-ci-physical.service \
  deploy/matrix-ci-physical.path:/etc/systemd/system/matrix-ci-physical.path \
  deploy/matrix-web.service:/etc/systemd/system/matrix-web.service \
  deploy/matrix-display.conf:/etc/tmpfiles.d/matrix-display.conf
do
  test "$(sha256sum "${pair%%:*}" | cut -d' ' -f1)" = "$(sha256sum "${pair#*:}" | cut -d' ' -f1)"
done

# Create and normalize the host-owned parent and canonical lock before either
# service is enabled or started. An existing canonical inode is not replaced.
/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/matrix-display.conf
test "$(stat -c '%U:%G:%a:%F' /run/matrix-display)" = "root:root:755:directory"
test -f /run/matrix-display/matrix-display.lock
test ! -L /run/matrix-display/matrix-display.lock
test "$(stat -c '%U:%G:%a:%h' /run/matrix-display/matrix-display.lock)" = "root:root:644:1"
```

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

Transfer that reviewed artifact and manifest through the administrator release channel, never through candidate CI. The installer descriptor-walks both source and destination ancestry with no-follow checks, requires root-owned non-writable regular single-link source files, rejects extra entries, verifies the independent manifest before creating the destination, and publishes only a fresh exact tree:

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
/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/matrix-display.conf
systemd-analyze verify /etc/systemd/system/matrix-ci-physical.service /etc/systemd/system/matrix-ci-physical.path /etc/systemd/system/matrix-web.service
systemctl enable --now matrix-ci-physical.path
systemctl is-enabled --quiet matrix-ci-physical.path
systemctl show matrix-ci-physical.service -p Type -p RuntimeMaxUSec -p CapabilityBoundingSet -p ExecStart -p ExecStop -p ExecStopPost -p Requires -p After -p ReadOnlyPaths -p UMask
systemctl show matrix-web.service -p InaccessiblePaths -p CapabilityBoundingSet -p Requires -p After -p ReadOnlyPaths -p UMask
systemctl cat matrix-ci-physical.service matrix-ci-physical.path matrix-web.service
/usr/bin/python3 -I -B /usr/local/libexec/verify-matrix-ci-physical-install \
  --dedicated-key-fingerprint SHA256:REVIEWED_DEDICATED_KEY_FINGERPRINT \
  --runner-address RUNNER_EGRESS_IP \
  --runner-hostname RUNNER_RESOLVED_HOSTNAME \
  --local-address APPLIANCE_SSH_ADDRESS \
  --local-port APPLIANCE_SSH_PORT \
  --routing-domain SSH_ROUTING_DOMAIN
```

Every SSH tuple value above must come from the real accepted connection: source address, sshd-resolved source hostname, appliance local address and port, and routing domain. The gate passes the complete tuple to `sshd -T -C`, because any of `Match Host`, `Match Address`, `Match LocalAddress`, `Match LocalPort`, or `Match RDomain` can change the effective key policy. These commands are installation instructions only; repository verification does not claim that this live appliance gate was run.

Before enabling protected CI, run the appliance-gated lifecycle acceptance that uses harmless substitute units and a temporary lock (never `matrix-web` or hardware): queued activation, dispatcher/SSH loss before and after activation, report-publication race, normal stop, runtime timeout, SIGKILL, exact-once verifier admission, bounded recovery, and a competing process failing to acquire the same canonical lock inode. If native construction raises before returning an object, a fresh driver blackout is explicitly best effort; a fake that succeeds on its second construction does not prove GPIO transfer or real-panel blackout. When the driver was returned but canvas creation fails, the verifier independently attempts brightness 1, driver clear, an obtainable canvas clear/swap route, and exposed close hooks while preserving the primary exception. Then perform separately approved physical acceptance. Repository/static tests do not prove installed PID-1 behavior, effective sshd policy, or hardware recovery.

## Protected GitLab variables

Configure:

- `MATRIX_DEVICE_TARGET`: a protected variable containing the fixed SSH target.
- `MATRIX_DEVICE_SSH_KEY`: a protected FILE variable containing only the dedicated private key.
- `MATRIX_DEVICE_KNOWN_HOSTS`: a protected FILE variable containing the pinned appliance host key.

Restrict every variable to the protected production environment/ref scope. They are never available to merge-request or branch jobs; the CI rule permits only a protected default-branch push. Independently inspect the live GitLab variable type, protection flag, environment scope, target protection, and non-exposure to unprotected refs before enabling the job. Do not print values or store them in repository files or artifacts.
