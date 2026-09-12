# Captain Linux browser feasibility gate

This stage supplies local browser lifecycle support only. No VM, posting path or
cloud integration is provisioned. The Windows development profile is **not** a
deployment artifact and must not be copied to Linux.

The future trial requires installed Google Chrome Stable, compatible Playwright,
Chrome's ordinary Linux libraries, a desktop/display for manual authentication,
and a session D-Bus with a functioning Secret Service (for example GNOME Keyring).
`gdbus` must be available. The default collection must already be unlocked in the
same user's session used for both bootstrap and unattended acquisition. The check
reads only collection metadata; it never enumerates items or reads secret values.
Chrome is explicitly directed to the libsecret backend. Metadata readiness alone
does not prove Chrome can decrypt its existing session: cold-boot acquisition is
the decisive trial. No keyring unlock automation is included here.

Configure an external persistent dedicated profile root, owned by the Captain
Linux user with mode 0700. Never use a personal/default browser profile. Installed
Chrome must be uniquely discoverable; competing installations require operator
resolution. Application runtime does not install or download browsers.

Under that user and secure desktop session, run `fpl-bot-captain-review-login
--profile-dir <dedicated-root>`. Manually complete the ordinary login flow, verify
premium Projections, and close Chrome. No public remote desktop service is added
by this application; protected administration belongs to the later trial.

The application ownership marker is exclusively created. Chrome singleton markers
are checked without reading their contents. A crash/uncertain close retains the
ownership marker and fails closed on reuse. Operators must establish that all
browser processes have ended and inspect the lifecycle before deliberate recovery;
the application never automatically removes stale browser locks.

The approved future no-post trial must prove: initial manual login; clean browser
closure; unattended fresh acquisition; full VM stop/start; automatic secure keyring
availability; another unattended acquisition; and clean shutdown. Also simulate
locked keyring, concurrent ownership and unclean shutdown. Use the same Linux user,
Chrome version and persistent profile/keyring environment throughout. Do not
export cookies, use storage-state APIs, disable encryption or downgrade the profile.

Audit timestamps describe acquisition start/end, never Review dataset generation.
Record platform, Chrome/Playwright versions, ownership and keyring states alongside
existing authentication/projection diagnostics. No assumption of exactly 50 rows
is introduced. Pagination/partial-load evidence requires review before deployment.
