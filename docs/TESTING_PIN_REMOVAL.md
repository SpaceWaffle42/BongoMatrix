# Temporary testing PIN removal gate

The pre-1.0 testing-access PIN is runtime state only. Its production path is:

`/home/admin/matrix/data/testing-pin`

The file is intentionally excluded from Git by the repository's `data/` ignore rule. It must contain only one six-digit decimal PIN with at most one trailing newline, be a regular non-symlink file with one link, and be owned by the Matrix service identity with mode `0600`.

## Pre-1.0 deployment checklist

- [ ] Provision the requested PIN only at `/home/admin/matrix/data/testing-pin`; do not place its plaintext in source, CI variables, service units, logs, or deployment manifests.
- [ ] Create it with an atomic owner-only operation, owned by the Matrix service identity and mode `0600`.
- [ ] Record the deployed source commit, application version, file owner/mode, and a checksum of the runtime file without recording its plaintext.
- [ ] Before activation, verify the exact application artifact accepts the file only for a version below `1.0.0`, while missing or unsafe files leave normal one-time pairing behavior unchanged.
- [ ] After activation, verify reusable testing access and normal one-time PIN expiry, single use, throttling, device-name validation, and paired-device limits without printing or rendering the testing PIN.

## Mandatory 1.0.0 release checklist

- [ ] Stop the Matrix web service before changing authentication runtime state.
- [ ] Delete `/home/admin/matrix/data/testing-pin` as part of the 1.0.0 deployment transaction.
- [ ] Verify with a no-follow metadata check that the path does not exist after deletion and after service restart.
- [ ] Verify the 1.0.0 application refuses testing-PIN authentication even if a secure file is deliberately restored for a negative test.
- [ ] Verify normal one-time display PIN pairing, expiry, single use, throttling, device-name validation, and paired-device limits still pass.
- [ ] Remove the temporary loader, `PairingManager` testing-PIN seam, related tests, and this note in the 1.0.0 source release after the negative migration test has been retained in an appropriate release test.

The application also fails closed at versions `1.0.0` and later, but that guard is defense in depth and does not replace deletion of the production file.
