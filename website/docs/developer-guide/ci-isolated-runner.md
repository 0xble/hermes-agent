# Isolated ARM64 CI worker — pilot, not production routing

## Observed first pilot and log transport

Run [34408550067](https://github.com/0xble/hermes-agent/actions/runs/34408550067)
executed successfully on JIT runner 21. The API reports 38,000 ms total duration and an empty billable map. The ephemeral registration removed itself. However, GitHub log download returned `log not found`: guest diagnostics contained proxy HTTP 403 upload failures. This was scheduling proof, not complete operational acceptance.

GitHub's [network requirements](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#communication-requirements-for-self-hosted-runners) require `*.blob.core.windows.net` for logs, summaries, artifacts and caches. The proxy now permits that suffix while retaining public-IP validation and exact-address dialing. The manual pilot also offers an explicit failure exercise; it cannot satisfy any required CI status.

Trusted runner authorization landed first in PR #123 (`19b64f4a1d5a307f9828e83fe31c13821b028044`). This change adds only a manual, non-required scheduling pilot. Heavy Python/JS jobs, real native OS lanes, coverage and required status names remain unchanged until full workload and disposable-worker acceptance passes.

## Independently enforced network boundary

A dedicated QEMU ARM64 VM (HVF, 4 vCPU, 8 GiB RAM, 30 GiB growable disk) uses libslirp `restrict=on,ipv6=off`. It has no host mounts, agent forwarding, container socket, host credentials, guest QMP/monitor or extra network device. This is NOT shared Colima/Lima NAT and NOT a guest firewall. QEMU's host-side restriction survives guest root adding a default route. No host PF/VPN/routes are changed.

The sole guest-originated connection is `10.0.2.100:18080`, forwarded to `scripts/ci/isolated_egress.py` on host loopback:

```
-netdev 'user,id=ci,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:18222-:22,guestfwd=tcp:10.0.2.100:18080-cmd:/usr/bin/nc 127.0.0.1 18080'
-device virtio-net-pci,netdev=ci
```

Use a fresh `nc` per connection. QEMU's persistent TCP chardev forwarding is NOT a reconnecting multi-client proxy; it stalled after its first connection closed in the live pilot. The SSH listener is host loopback only, with a newly generated management-only key; no SSH agent is forwarded.

The external proxy accepts only CONNECT to HTTPS port 443 for an explicit dependency-domain allowlist. It rejects IP literals and unapproved authorities before DNS. It resolves through TLS-verified Cloudflare DoH at fixed public IP `1.1.1.1`, rejects non-global answers and connects to the exact vetted address, without system resolver reuse. This avoids this Mac's VPN synthetic-IP DNS without changing VPN settings. TLS payloads remain opaque. The proxy is a network boundary, not protection against all QEMU vulnerabilities or host process-resource exhaustion; do not treat it as a hardened multi-tenant service.

## Verified pilot evidence (2026-09-09)

Local evidence is `/Users/brianle/.hermes/cache/ci-isolation/` (not uploaded because it contains a management key). `isolation-proof.json` records a guest-root default-route bypass attempt: host-loopback canary, host/private LAN, tailnet, link-local metadata, direct public TCP, UDP DNS and IPv6 denied; GitHub, PyPI and npm registry through the proxy returned 200. No shared mounts or agent forwarding were present. `verify_guest.py` is the probe source. `bootstrap.log` records ARM64 Ubuntu package and checksum-pinned uv/runner downloads. `python-workload.log` and `js-workload*.log` are real workload attempts, not acceptance by themselves.

An x86-only ripgrep pin is replaced by a checksum-pinned architecture switch. The ARM64 15.1.0 artifact was downloaded, hash-verified and executed in the VM. Unknown architectures fail closed. Both existing Python workflow installs use the same script.

## Lifecycle acceptance still required

Registration must be repository-scoped and ephemeral/JIT. Only its short-lived runner credential may enter the guest, never a host PAT, gh config, cloud credential or SSH private key. Keep the clean image immutable, run every job on a disposable overlay, deregister stale runners, stop the host QEMU process and discard the overlay on success, failure, cancellation or timeout. Queue timeouts must cancel unmatched pilot runs. Do not enable production labels/routing until those behaviors and full ARM64 workloads are proved. Self-hosted scheduling is tested explicitly; hosted billing exhaustion alone is not evidence that it is blocked.

## Exact rollback scope

For this development pilot only, stop the QEMU PID recorded in `qemu.pid` after matching its command/path, stop only the dedicated proxy process/listener, deregister only runner IDs created by this pilot, and cancel only its outstanding workflow runs. Remove only this VM's disposable disk/seed/keys after collecting receipts. The shared Colima default and its writable mounts, host PF, routes, VPN, other runners and dotfiles are out of scope and untouched.

QEMU was installed using Homebrew; Homebrew also upgraded its `p11-kit` dependency despite `HOMEBREW_NO_INSTALL_UPGRADE=1`. Do not attempt a blind shared-dependency downgrade. No budget or billing change was made. No monthly spend outcome is claimed before workload duration and actual billable-usage measurements exist.
