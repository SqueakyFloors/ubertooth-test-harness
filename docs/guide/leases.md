# Sharing boards: leases and locks

How concurrent runs and by-hand commands share the bench.

## Sharing boards: leases

Several agents can share one host's Ubertooths when they run as the same user
account (leases live in that user's home; separate accounts do not exclude
each other, see [#12](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/12)). `run-suite` leases
every board it touches before preflight and holds the leases until the run
ends: the selected board, guard and alternated boards, power-isolated boards,
and, for `Flash reliability` and exclusive tests, every Ubertooth on the host
(they read or enumerate all of them). A lease is an exclusive `flock` on
`~/.ubertooth-harness/leases/<serial>.lock` (override with
`UBERTOOTH_HARNESS_LEASE_DIR`); the kernel drops it when every process holding
that file exits, and a lease left behind by a session that is gone is taken
over (below). Boards are taken all or nothing, so two runs cannot
deadlock. A leased board needs its full 32-hex-digit serial in the hardware
registry: libubertooth matches `-U` against the start of a serial, so a short
serial would select the same board under a different lock.

One lease file is one device, so the registry is refused when two resources
would take the same one
([#288](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/288)).
A lease key comes from one identity field — a board's serial, a DK's J-Link
serial without its leading zeros, a peer's bridge serial, a programmer's
serial — and the duplicate-identity check compares whole identity mappings,
so two resources whose other fields differ could otherwise take one file
while everything that read the registry treated them as two devices (a run
binding both ends of a connection to one DK, with the lease saying nothing).
The serials themselves are compared the same way, because a device's external
lock is named after its plain serial: one serial is one device, whichever way
it is written, and on whatever type it is registered.

If a board is held, the run fails before any build or device command and names
the holder. `--wait-for-boards MINUTES` waits instead; `--owner LABEL` (or
`UBERTOOTH_HARNESS_OWNER`) names you in other agents' errors. The run report
lists the leases.

```sh
ubertooth-harness boards --hardware hardware.yaml --host Host_Mac  # free / held by whom
ubertooth-harness lease --hardware hardware.yaml --owner "agent #14" UT_Mac0 -- \
    ubertooth-util -U <serial> -v                          # manual use, leased
```

`lease` holds the named boards for exactly as long as the command runs; the
command inherits the locks, so they stay held even if the harness process is
killed first (and once that process is gone, the next command that wants the
board may take it over: see "Stale leases" below). Use it for every manual
Ubertooth command on a shared host.

### Stale leases: a session that is gone

Because the command inherits the lock files, a lock lives as long as any
process holding one of them. A session killed part way through — a dropped
network connection, a batch kill — can therefore leave a lease nothing is
waiting on: the harness process is gone, and something it started, re-parented
and forgotten, still holds the lock. A board locked like that used to stay
locked (five hours of a release soak,
[#215](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/215)).

So a command that finds a busy lease recorded by a process of *this* host
that no longer exists takes the board over: it removes that lock file, which
is what later commands read, and says so in one line on standard error.

```
note: UT_Linux5 was held by a lease that no longer holds it: its process
(pid 2380347) is gone, and so is the process group it ran in (2380347).
Taking it over (/home/.../leases/05f0....lock).
```

A lease recorded on another host is left alone (a pid there means nothing
here), as is a pid the system has since given to an unrelated process.
`<lease dir>/takeover.lock` orders a takeover against everything else: a
takeover holds it exclusively, and every command holds it shared while it
takes its locks and checks them, so a lease file is never removed while a
command is taking it and two commands can never end up holding one board
through two lock files.

What a takeover does not promise: something still holds the lock, or the
kernel would have dropped it, and that something may still be using the
board. The USB device is what really excludes two users — a second claim of
an Ubertooth fails — so a leftover capture makes the next device command fail
loudly rather than quietly sharing the board; a wrapper that only held the
lock while doing something else costs nothing. That is the trade the bench
wants: the board comes back, and the note says what to look for. When the
record's process group is still running, the note names it, because a
leftover command that session started may still be talking to the board:
`pgrep -g <group>` shows what to stop. That signal only covers the process
group the command ran in, so a leftover that left it (`setsid`, a daemon) is
reported as gone.

A takeover costs a poll round like any other busy lock, so a lease file that
keeps coming back cannot make a command spin. A board whose lease could not
be taken over (another command was taking one over at that moment) is
reported with the same explanation the host lock gets below.

A `run-suite` that took a lease over carries that into its run record: a
`lease_takeover` record in run.jsonl (the board, the record of the holder
that was gone, why it was read as gone, and the lock file), a line in the
transcript, and a "Leases taken over" row in report.md once the run reaches
its manifest (a run that fails in its build has the record and the
transcript line). Since a takeover does
not promise the board was idle, that is what to look at when a later result
in the run looks odd — and a run started by a deployed runner leaves no
other trace, because the line on standard error goes to the runner's own
log. `lease` keeps that line and records nothing else: it is a person at a
terminal. (`boards` never takes a lease over at all.)

The host lock is never taken over. It is the only thing that keeps USB power
changes and firmware writes on a host from overlapping, and what still holds
its file could be a firmware write in flight — a process that left the
recorded session, or one of a session the record does not name at all. So its
error says what is known and what to look for, and a person decides:

```
error: this host's power/firmware lock is in use: held by agent, lease:
ubertooth-dfu -U ... -d ..., pid 4471, since ... UTC (.../host-power.lock);
the command that took it is gone: its process (pid 4471) is gone, and so is
the process group it ran in (4471); it is never taken over, because a
firmware write or a power change it started may still be running: look for
one, and remove the lock file once you know there is none
```

`boards` marks such a lease and removes nothing itself:

```
UT_Linux5    held  agent, lease: bash soak.sh, pid 2380347, since ... (for 5h12m)
             stale: its process (pid 2380347) is gone, and so is the process
             group it ran in (2380347); the next command that wants it takes
             it over
```

### Waiting in turn

Retrying alone is not fair. A run that wants the host lock *and* a board
could wait out its whole `--wait-for-boards` while short board-only leases
took that board one after another: each attempt found the board busy, gave
back the host lock, and tried again, and nothing held the newcomers back
([#202](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/202)).

So every command first takes a numbered ticket for each board (and the host
lock) it wants, under `<lease dir>/tickets/<key>/`, and gives way to any older
ticket of a command that is still waiting. What that promises, exactly: a
command does not take a board out of turn in the moment it comes free, so the
command that has waited longest gets a clear run at it, and one that asks for
a board somebody has waited longer for is told so instead of taking it:

```
error: UT_0 is queued: agent #14, run 2026-10-31-ble, lease: ubertooth-util
-U ... -v, pid 7781, since 2026-10-31T06:12:03.114 UTC asked for it first and
is still waiting for it; use --wait-for-boards MINUTES to wait for your turn
```

A run that wants several boards (and the host lock) is served the same way,
and that takes one thing more: boards are taken all or nothing, so such a run
needs every one of them free in the same attempt, and with short leases
arriving on different boards each can be free in its turn and never all at
once. So each ticket records the whole set its command is waiting for, and a
command gives way to an older ticket for as long as anything in that set is
held, without the bound below. The boards that are free stay free while the
run waits for the one that is not, until it can take them all; the error a
newcomer gets says which board that is:

```
error: UT_1 is queued after waiting 5s: agent #3, run 2026-10-31-flash,
run-suite release / comprehensive, pid 8120, since ... UTC asked for it first
and is still waiting for it; that command is waiting for UT_0 too, which is
held by agent #14, run 2026-10-31-ble, pid 7781, since ... UTC; use
--wait-for-boards MINUTES to wait for your turn
```

A ticket is a file named after its number, locked while its command waits and
removed when it stops waiting. It is locked and written before it appears
under its queue name, so a ticket counts only while its command is really
there: one nobody holds is nobody's and is removed, as is one whose process
is gone, and those are the only things `boards` takes away. It shows how
deep a queue is (in `--json`, as `waiting`):

```
UT_0         held  agent #14, run 2026-10-31-ble, pid 7781 (for 0h04m11s)
             queue: 2 commands are waiting for it
```

Giving way is bounded in one case: the command ahead could take everything it
asked for right now — all of it is free — and has not. After half a second of
that — two poll rounds, enough for a waiter that is really waiting to notice a
board was released — a command competes for the board as it did before the
queue, once for each command ahead of it; a command with no
`--wait-for-boards` has a whole second before it gives up, so it too ends up
competing rather than failing (and, if the board is taken again just before
that second is up, failing with `is queued` where it used to say `is in
use`). Where the command ahead is waiting for something it cannot have yet,
there is no such stretch: a command with no `--wait-for-boards` then fails
with `is queued` after its second rather than competing. The bound is per
stretch of giving way, and per board: it starts again whenever one of
those boards is held, or the command ahead in the queue changes, so a long
wait never turns into a license to jump the queue. Giving way to a command
that is waiting for a board somebody is using is never bounded: nothing in
the harness can be stuck on a lock that is held, so the waiters simply take
their turns.

That bound is what keeps the queue from becoming a new way to wait forever. A
command can be ahead in the queue, with everything it asked for free, and
still not take it, for a reason the queue can neither see nor help: a wrapper
outside the harness holding a board's own lock file while it waits for the
lease (the order below). Its neighbors give it a clear run at the board and
then carry on. A command gives way at each board in its own right, so waiting
the command ahead at one board out never lets it take a board another command
has been waiting longer for; within one board's queue it is the command that
has waited longest that gets the turn, as before. The host lock is the other
thing a waiter can be held up by with no way through, since it is never taken
over (above): one that no running command holds counts as free here, so a
lock a person has to clear does not hold the whole bench in the queue.

What a multi-board run costs the others, in exchange: while it waits for the
last board it needs, a command that wants one of the boards already free
waits too, and one that asked to wait for nothing fails with `is queued`.
That lasts as long as the run's own `--wait-for-boards`.

### Why a board can be free and still nobody's

Usually the wait above is seconds, because what holds the last board is a run
that ends. It is the whole of `--wait-for-boards` when the lease in the way
never comes back on its own: a board held by a command that is running but
stuck, or one whose lease was recorded on another host or holds no record at
all, which the harness never takes over. The bound stays the waiting
command's own deadline
([#234](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/234)) —
capping it instead would hand the bench back to short leases and starve the
multi-board run again — and what the others get is the reason, in words.
`boards` prints it under a free board:

```
UT_0         free
             queue: 1 command is waiting for it
             queued for flash-run, run 2026-10-31-flash, run-suite release /
             comprehensive, pid 8120, since ... UTC: it asked for UT_0 and
             UT_1, has waited 4m12s of the 10m00s it asked to wait, and is
             still waiting for UT_1, held by elsewhere, pid 7781, since ...
             UTC (that lease is left alone: it was taken on another-host, not
             on this host; it stays held until somebody clears it)
```

A person reads which command has the turn, everything it asked for, how long
it has waited of what it asked to wait, and the board in its way — with what
that board's lease records, which is what says whether the board comes back
on its own (its command is gone, so the next command that wants it takes it
over) or whether somebody has to clear it (another host's record, or none).
In `--json` it is `queued_for`. Each line is read from the waiting command's
own ticket every time, so it is gone the moment that command stops waiting,
and a ticket whose command died leaves nothing behind. No lock file path
appears in any of it: the lease directory is private, and the resource and
the command are what a reader needs.

A command that is giving way says the same thing itself, once that board has
been free and queued for more than two seconds, so two commands sorting
themselves out are not reported as a bench standing still (`boards`, which
answers a person asking why nothing is running, shows it as soon as it is
true). That makes a `lease` that sits there explain itself on standard error,
and `run-suite` carries it into its run record: a `lease_queued` record in
run.jsonl, a line in the transcript, a "Queued behind another command" row in
report.md, and the same line in the browser's live view. A run whose first
test started ten minutes late has to be able to say why. Those read in the
past tense ("UT_0 was free and queued for ..."), because the run has its
boards by the time anybody reads them, and each says how long the run gave
way at that board while it was free; the record is kept up to date while the
wait lasts, so it is that stretch, not its first moment.

### The host lock

Every harness command that switches USB power or writes firmware also holds
the host lock, `<lease dir>/host-power.lock`, the same kind of `flock` lease
(released by the kernel when its holders exit, and taken over when they are
gone, as above): a
`run-suite` with power isolation (StarTech or Acroname) or an exclusive
Ubertooth check, `Flash reliability`, or `DFU images` holds it for the whole
run, a `--build` run that flashes its boards with its own build takes it with
its leases before the build and releases it after the flash stage (so it is
held for the build as well, and two such runs on one host do not start at the
same time), and `lease` holds it for its command with `--host-lock`, or on its
own when the command line names `ubertooth-dfu`. A second command that needs
the lock fails at once, before any build or device command, and names the
holder (owner, run, command, pid, and since when); `--wait-for-boards` waits
for it as for a board. The run report records it. Runs that neither switch
power nor write firmware do not take it, so they still share the host.

What counts as naming `ubertooth-dfu`: the tool anywhere in the command line,
so `sudo ubertooth-dfu ...`, `env VAR=1 ubertooth-dfu ...` and
`sh -c "ubertooth-dfu ..."` all take the lock, matched by a word's last path
component, which leaves out `ubertooth-dfu-wrapper`. `lease` says on standard
error when it asks for the lock this way. Only the text is read, with the two
ways that is wrong settled in favor of the lock: a command that reaches the
tool without naming it — through a variable, a wrapper script of its own, or a
list of commands read from a file — needs `--host-lock`, and one that merely
mentions the name holds the lock for as long as it runs, which delays other
commands and breaks nothing.

`ubertooth-harness boards` shows the lock as `host lock (this machine)`: it is
the lock on the machine the command runs on, whatever `--host` names, since a
lock file read here says nothing about another machine's. In `--json` it is
the entry with `host_lock: true`. The board lines are local in the same way,
which is why `boards` asks for the host it is running on: name another
machine's host and its boards are listed from this machine's lease directory,
where nothing holds them.

Lock order: the host lock first, then the boards in serial order, taken in one
all-or-nothing attempt, and behind the queue above. Nothing blocks while
holding a lock (a busy lock releases everything and the attempt is retried
until the deadline), and no
command takes the host lock after it already holds a board, so runs and
manual leases cannot deadlock. With external board locks (below) the full
order is: external board locks, host lock, leases.

### External board locks

A lease only excludes other harness commands. If a lab's own scripts lock
boards with their own per-board `flock` files, give the harness their path
as a template and every lease takes that board's file too:

```sh
export UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK='/var/lock/board-{serial}.lock'
# or, per command (lease, run-suite, boards, runner):
ubertooth-harness lease --external-board-lock '/var/lock/board-{serial}.lock' \
    --hardware hardware.yaml UT_Mac0 -- ubertooth-util -U <serial> -v
```

The template is an absolute path (or starts with `~/`) with exactly one
`{serial}`, replaced by the device's plain serial, which is what a lab's own
script was written against: an Ubertooth's full 32-hex-digit serial in
lowercase, a DK's J-Link serial as registered, a Classic peer's bridge
serial, a debug programmer's serial. That is not always the device's lease
key, which carries its type's prefix for everything but an Ubertooth
(`nrf52840dk-<J-Link serial without leading zeros>`, `esp32-<bridge serial>`,
`programmer-<serial>`); the external file keeps the plain serial so that a
lab wrapper and a harness run meet on one file
([#317](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/317)).
A host Bluetooth adapter has no serial at all — it is leased as
`bluez-<address without its colons>` — so it has no external file and no
second name. Every path that leases a device asks for both names — a run,
`lease`, `boards`, `runner`, and the helpers in `scripts/lab`. No other brace;
`--external-board-lock` overrides the variable and
`--external-board-lock ''` turns it off for one command. Unset, nothing
changes. A bench set up before
[#317](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/317)
may have `board-esp32-<serial>.lock`, `board-nrf52840dk-<serial>.lock` or
`board-programmer-<serial>.lock` files left over from when a run asked for
the lease key: nothing takes those now, so delete them and make sure the
lab's own wrappers lock the plain-serial file, which is the one every harness
command takes. One spelling matters: the file is named after the serial as the
registry spells it, so a DK's is the padded J-Link serial if that is what the
registry carries, not `nrfutil`'s zero-stripped form
([#324](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/324)). Each external lock is an exclusive `flock` on that file, taken in
the same all-or-nothing attempt as the leases and dropped by the kernel when
the holder exits, like a lease. A missing file is created (mode 0644, an
existing file keeps its mode and content; the harness opens it read-only and
never writes to it), but a missing directory is an error, so a mistyped
template cannot start a lock tree nobody else uses. A board whose external
lock is held fails the run (or waits, with `--wait-for-boards`) with
`UT_0 is in use: held outside the harness: <path>`, followed by the first
line of the file if it holds any text (any format; a JSON object is
described like a lease's holder). `boards` shows such a board as `held
outside the harness: <path>`, and the run report lists the external locks
the run held. A file the harness cannot open for reading (another user's
0600 file, say) stops the command at once, naming the board and the file:
waiting would not help. `boards` takes such a board as held, since unknown
is not free. `runner` passes the option to every run it starts
(`deploy.sh --external-board-lock`, which is how a deployed runner gets it:
the generated units carry no environment, so the variable alone does not
reach it).

Full lock order, one hierarchy for every command: a ticket per resource for
as long as the command waits and `takeover.lock` shared for as long as each
attempt lasts (both above; neither is a resource, and nothing ever blocks on
either), then the external board locks in serial order, then the host lock,
then the board leases in serial order.
A lab wrapper that takes a board's own lock file and then runs
`ubertooth-harness lease` (which may take the host lock, then the leases)
follows the same order, so the two cannot deadlock; the harness also never
blocks while holding a lock, so it cannot be half of a cycle whatever another
tool does. A wrapper that takes the harness lease first and only then blocks
on the board's own file inverts the order: nothing deadlocks, but the harness
spins until its deadline, so take the board file first. Do not set the
template for a command that such
a wrapper runs: the wrapper already holds those files, so the command would
find them busy and refuse (it fails, it does not hang). Use
`--external-board-lock ''` there, or drop the wrapper and let the harness
take both locks.

### A device and its debug programmer: one pair

A bench may leave a programmer wired to one board's debug or programming
header (a USB serial adapter, an SWD probe). The two are one piece of
hardware as far as using either goes: programming the board while another
command owns it, or using the board while it is being programmed, spoils a
test or leaves the board needing hands on it. So the registry records the
pairing, and every command that holds one member holds the other
([#286](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/286)):

```yaml
  Prog_Linux0:
    type: Debug programmer
    attached to: Host_Linux
    programs: UT_Linux1          # the one board it is wired to
    identity:
      serial: A000AA00           # leased as programmer-A000AA00
```

Whichever member a command names, both are taken in the same all-or-nothing
attempt, in the usual lock order, and naming both is harmless. That holds for
`ubertooth-harness lease`, [`scripts/lab/hold`](../../scripts/lab/README.md), a
`run-suite` that binds or power-cycles the board, and
`scripts/lab/hub-port` on either member's port. The three commands a person
runs by hand say on standard error which device the pairing brought in and why
(`note: Prog_Linux0 is the programmer wired to UT_Linux1, so it is held
too`), so a holder nobody asked for is never a mystery, and `boards` prints
the pairing under both devices. A run says the same line on standard error
before it takes the locks, and carries it in its record: a `lease_pairing`
record in run.jsonl (the device, and the registry's own clause, which names
both), a line in the transcript, a "Leases from a wired pair" row in
report.md, and the same line in the browser's live view, so a reader of a
report never finds a programmer among the leases of a run that asked for a
board ([#289](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/289)):

```
Prog_Linux0  free
             pair:  UT_Linux1 is the device Prog_Linux0 programs, so the two are held together
UT_Linux1    free
             pair:  Prog_Linux0 is the programmer wired to UT_Linux1, so the two are held together
```

The registry is refused when a pairing cannot be real: a target that is not
registered, is not a device the harness leases, sits on another host (leases
are local to one host), or already has a programmer, a programmer that names
itself or more than one target, a `programs:` on anything that is not a
`Debug programmer`, and a pairing that loops back on itself. A chain of pairs
is allowed and is held as one set. No test binds a programmer and the harness
never drives one; see
[`docs/hardware.md`](../hardware.md#a-debug-programmer-and-the-board-it-programs).

