---
audience: human
doc-type: guide
based-on: Operator-run Octopus bridge task; existing ear-ingest octopus-finalized and OctopusInboxChannel
---

# Octopus finalized export bridge

`ear-ingest octopus-bridge` is an opt-in, one-shot producer for uploads that land in Octopus instead of `ear-ingest`. It can queue a small batch locally or emit one verified wire frame without access to the receiver's filesystem. It does not install a hook, monitor a directory, migrate history, call old Ear, or start recognition. A durable receipt means **native intake accepted the record**, not that recognition or downstream writes succeeded.

## Activation prerequisites

1. **Confirm ownership and times with the Octopus operator.** Prefer a trusted hook after Octopus has durably finalized the raw upload. If the hook already sends the wire format, use `ear-ingest octopus-finalized` directly. Otherwise use a dedicated raw export as described below. The [Brain file reader](../adapters/README.md) provides pinned-file checks, but the generic Brain layout does not establish ownership, finalization, or whether its timestamp is a session or segment start. **A legacy-Ear-owned mount or unverified inbox is a blocker, not a fallback.** Do not point the bridge at either or add a marker to one. If the private receiver cannot provide trusted finalize metadata and a hook or dedicated export, activation is blocked. No deployment mount or private hook was verified for this change.
2. Provision a **new**, private, flat, Octopus-owned directory for one device and activation. Use a trusted POSIX filesystem or a read-only view preserving ownership and file identity. Root, marker and selected files must have the specified Octopus UID; group read/traverse is allowed, group write and all access by others are not. Use e.g. `0700`/`0600` for one trusted account or `0750`/`0640` for a designated reader group. The root path must be absolute with no symlinked ancestors; selected files must be regular and single-link. Protect ancestors and mounts from untrusted changes. The marker and UID checks enforce the configured boundary; they cannot prove provenance if the operator selects the wrong mount or trusts the wrong account.
3. Set `EAR_INGEST_OCTOPUS_DEVICE_ID` to the phone device ID and `EAR_INGEST_OCTOPUS_FINALIZED_AFTER` to this activation instant with a known offset (prefer UTC `Z`). Set `EAR_INGEST_NATIVE_ROOT` for local delivery or on the receiving host only, using the same private volume as the native consumer and no legacy notification URLs. Choose an explicit lookback of 1–1440 minutes and synchronize clocks. Use private scratch space with room for the frame, receiver verification copy and intake spool write. Neither CLI needs a bearer token.

## What Octopus publishes

- `scope.json` with exactly `format`, `device`, `enabled_after`. Example shape: `{"format":"octopus-raw-finalized-export.v1","device":"test-phone","enabled_after":"2026-11-27T15:00:00Z"}`. Use the actual activation instant, not this example or a retroactive date. Compare device IDs case-insensitively and compare cutoffs as instants.
- One opaque, random slot of **32 lowercase hex characters** per record, including sidecars. `<slot>.raw` holds the final bytes; `<slot>.json` holds exactly the single LF-terminated UTF-8 line of the [existing `record.finalized.v1` wire format](README.md#octopus-手机上传和独立落地). Send the actual phone device, case-preserved group and original segment index. Do not send paths. `size` and `sha256` describe the complete bytes. Media and kind support are those of the native intake.
- Only the trusted producer supplies `started_at`, `ended_at` and `finalized_at`. Use exact physical segment bounds with known offsets and the actual Octopus finalize instant. Do not use filename clocks, mtime, or automatically add offsets to the Brain `timestamp`. Do not export a record without authoritative times. The segment end (or permitted point record's start) must be at or after activation and no later than finalization; finalization must be within lookback and not in the future. The stdin receiver independently enforces its cutoff and 24-hour maximum age.
- Stage outside the export directory on the same filesystem. Publish the complete raw file first and JSON last with atomic renames and durability sync; keep the pair immutable until acknowledged. Keep at most **32 slots / 65 entries**, including the marker, with no subdirectories, temp files or unrelated entries. A raw-only or manifest-only slot may be seen mid-publication. The bridge is read-only at the source; Octopus must retain, retire and alert on export entries.

## Run locally

After provisioning a new post-enable export and configuring the environment above:

```sh
ear-ingest octopus-bridge \
  --export-root "$OCTOPUS_FINALIZED_EXPORT" --octopus-uid "$OCTOPUS_EXPORT_UID" \
  --lookback-minutes 30
```

The command enumerates at most one flat directory before delivering anything. Overflow or an unexpected entry name rejects the run. It opens raw bytes only for in-window manifests, verifies length and SHA-256, rechecks file identity and submits the verified snapshot through finalized intake. `EAR_INGEST_NATIVE_MAX_BYTES` bounds the payload (128 MiB default, 512 MiB maximum; media limits can be smaller).

Stdout for a completed enumeration is a JSON `slots` map keyed by opaque slot. Only `accepted`, with a durable `record_id`, lets the operator retire that slot. Retry `pending` (incomplete pair, size/hash mismatch, change during snapshot or unconfirmed intake) inside the window. Investigate `rejected`; `outside_window` was skipped without opening raw bytes. Exit 1 indicates pending work or an unavailable export; exit 2 indicates a rejection. Exit 0 does **not** acknowledge skipped slots. Partial success is possible. If interrupted or a receipt is lost, replay unchanged bytes within the window. On expiry, alert; do not extend eligibility or re-stamp old events.

The same device/group/index/kind identity serves direct upload and this bridge. Matching deliveries converge on the same native `record_id`. Do not intentionally dual-publish: disagreeing start times or bytes can conflict, and the native intake preserves the metadata of the first acceptance.

## Cross-host store and forward (proposal; not deployed)

On the Octopus host, add `--emit "$SLOT"`. The bridge looks up only that slot, verifies it and writes a binary frame to stdout. Exit 0 means **emitted**, not received. This mode needs no queue configuration. On the receiving host, consume a private frame with `ear-ingest octopus-finalized < "$FRAME"`. Never send a frame to a terminal or log sink.

Retain immutable export pairs as a bounded durable outbox, or write emitted frames to private quota-limited staging and sync/rename only after emission succeeds. Bound count, aggregate bytes, age and concurrency; stop and alert rather than silently evicting. Use a dedicated authenticated, encrypted channel, e.g. host-key-pinned SSH with a separate least-privilege key and fixed receiver wrapper. Allow only the receiver command with fixed scope/queue settings: no shell, PTY, agent or port forwarding, or caller-selected paths. Enforce the configured lookback (or tighter), per-frame byte limit and deadline on the receiver. Keep credentials out of arguments, logs and Git. No shared receiver mount or mobile upload credential is needed.

Send one frame at a time with bounded backoff within its original window. Treat a timeout, missing or malformed receipt, or nonzero receiver exit as unconfirmed. Retire only after the **receiver** exits 0 and returns its durable receipt for that invocation. Alert on expiry and conflicts without re-dating or broadening the scope. No remote wrapper, listener, credentials or schedule is installed by this change.

## Synthetic proof

`uv run pytest services/ingest/tests/test_octopus_bridge.py` runs a synthetic raw export and native spool through local queuing and the installed CLI frame producer/receiver without an Octopus mount or old Ear service.
