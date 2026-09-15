# Windows one-shot no-post trial

This is a feasibility test, not production scheduling. No X/cloud clients or
credentials belong in the browser bundle. Reuse installed stable Chrome and the
existing external dedicated profile **in place**, under its existing Windows user.
Never reset the account password, use SYSTEM/S4U/auto-login, transfer browser state,
or automate authentication. DPAPI/Chrome access under a batch logon remains an
empirical gate; a successful interactive run is not proof of non-interactive access.

## Runtime and audit

Use the Python 3.13.12 virtual environment already installed for the controlled
trial, with Playwright 1.62.0 and tzdata 2026.3. No activation or PATH configuration
is required: use the absolute virtual-environment python.exe. Extract a reviewed
minimal source bundle into a new application subdirectory, never into the profile.
Confirm its hash and `python.exe -m pip check` before registration.

Action: absolute virtual-environment `python.exe`.
Arguments (substitute the reviewed absolute paths and existing account name):

```text
-m fpl_bot.captain_worker_trial --profile-dir "<dedicated-user-data-root>" --output-dir "<new-result-directory>" --expected-user "<existing-user>"
```

Set **Start in** to the extracted directory containing `fpl_bot`. The output
parent must exist; the output directory itself must not exist. The runner creates
it exclusively before any network access. An existing directory blocks every
subsequent run, including after a crash. Do not delete it to retry automatically.
On Windows, the result directory is created atomically with a protected DACL
granting only the process token's actual account SID, SYSTEM and Administrators
full control. Its owner is explicitly the account SID; inheritable ACEs give the
audit file the same explicit account access even if its default owner differs.
No account name is hardcoded. This avoids Python's Windows `0700` OWNER RIGHTS
behavior, which produced an Administrators-owned result inaccessible from the
later non-elevated interactive session during the successful cold-boot proof.
Linux retains exclusive `mkdir(mode=0o700)` behavior. The fix does not repair or
change existing result directories, browser profiles, or global permissions.
No PowerShell redirection is used. `audit.json` is written directly as UTF-8,
with an exact tweet, ownership for each selection, original source-row ordinals,
separate projection ranks, acquisition timing, counts and safe failure categories.
An incomplete/missing JSON file is NOT success. Stdout/stderr are not used as an
audit or copied into the result; arbitrary exception messages are never persisted.

`execution_user` is a process-username sanity check, not independent proof of a
Windows security token. Corroborate it with the registered task principal and
Windows logon/task events. No special user-profile loading code is used; require
the password-backed task logon to provide its normal Windows user environment.

## Private registration stop point

Prepare files and inspect configuration first. STOP before saving/registering the
task: its owner must enter their existing password privately in Windows' native
Task Scheduler dialog. Never put the password in a script, argument or transcript.

In Task Scheduler **Create Task**, use a unique Captain trial name:

- General: existing profile-owning account; **Run whether user is logged on or
  not**; leave **Do not store password** unchecked (not S4U); leave **Run with
  highest privileges** unchecked. This may require approved administrator action
  or batch-logon rights; if denied, stop, do not change security policy blindly.
- Trigger: **One time**, approximately 30 minutes ahead in the VM's verified local
  timezone; no repetition; expire 45 minutes after the chosen trigger. Record the
  chosen local time and its UTC conversion before saving.
- Action: the absolute Python/arguments/working directory above.
- Settings: allow missed-start execution, no failure restart, **Do not start a
  new instance**, stop after 10 minutes. Forced termination is a failed trial and
  requires a lock audit; it is never proof of clean shutdown.
- Conditions: require network availability. No recurring boot/logon trigger.

Confirm registration privately, then inspect the task's Password logon type,
principal, trigger, arguments, working directory, expiry and settings without
exporting credentials. Do not click Run for an intermediate acquisition.

## Cold-boot sequence and evidence

Before stopping, confirm no Chrome processes or profile lifecycle markers remain.
Record the current task LastRunTime/LastTaskResult and upcoming trigger. Verify the
Compute Engine four-hour STOP safeguard. Stop the VM and independently verify
TERMINATED; close RDP and the user-managed IAP tunnel. Start the same VM sufficiently
before the trigger, but **do not connect with RDP** until after the trigger plus
the bounded 10-minute execution window. Never claim this gate passed on an
interactive-login run or on a manually triggered task.

Afterward collect only public/sanitized evidence:

- VM last start and OS boot time, scheduled time, task last-run time/result;
- Task Scheduler Operational execution events for this named task;
- Windows logon evidence since boot: matching task account's batch logon (type 4),
  and absence of interactive/remote-interactive logons (types 2/10) before execution;
  session-manager reconnect events if applicable. Inspect only time, account and
  logon type, not full unrelated security logs. If logs are unavailable, explicitly
  report that independent no-RDP evidence is incomplete.
- `audit.json` strict UTF-8 decode/JSON parse, success/exit 0, complete counts,
  exact selections/tweet, independently recomputed weighted length;
- zero Chrome processes, released ownership and absent Captain/Singleton markers.

If authentication fails, stop with the safe error; do not log in or repair state
within this run. If any lock survives, do not delete it. Stop the VM after clean
shutdown and verify TERMINATED. Disable the completed one-shot task deliberately
after recording evidence, with user approval; its output guard already prevents
another acquisition. No production integration is authorized by this procedure.
