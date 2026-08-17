# otop

**htop/btop for Odoo.** A compact terminal monitor for Odoo servers: worker
processes, PostgreSQL, storage and server load, for LIVE and STAGING side by
side.

```
otop
```

No web dashboard, no daemon, no agent, no Docker, no Redis, no Prometheus, no
external service, no database of its own. One process, one terminal, curses from
the Python standard library.

```
OTOP 1.0.0  odoo-prod up 41d02h    LIVE   STAGING                        09:14:22
CPU  [|||||||           ]  38%  cores 8 .=:. -#:  LOAD 2.41 2.02 1.88
RAM  [|||||||||||       ]  62%  9.9 GB / 16 GB  free 6.1 GB
SWAP [||                ]   8%  0.6 GB / 8.0 GB

ODOO WORKERS  prefork
TOTAL 8   BUSY 6   IDLE 2   UTIL 75%   AVG 41%   QUEUED 2   (avg of last 30 samples)
PID     ROLE       CPU      RAM    UPTIME STATUS     BUSY%  DB
12400   master      0%   162 MB    41d02h -             -  -
12401   http       82%   420 MB    12h04m * BUSY      68%  active 0.4s
12403   http        2%   390 MB    12h04m o IDLE       9%  idle
12409   cron        1%   175 MB    12h04m o IDLE       2%  idle

STORAGE
/     [||||||||||||      ]  64%  320 GB / 500 GB   free 180 GB
  LIVE database             82 GB  16.4%
  LIVE filestore           142 GB  28.4%  4m12s ago
  Other files               96 GB  19.2%  disk used minus the rows above

POSTGRESQL  odoo_live  82 GB
CONN 86   ACTIVE 18   IDLE 64   IDLE-TX 4   WAITING 4   LONGEST 12.4s
  pid 8821    12.4s  UPDATE stock_move SET state = $1 WHERE id IN ($2, $3, ...
  account_move_line             41 GB   idx 12 GB

DISK   R 42.1 MB/s   W 18.0 MB/s   (620/210 iops)   IOWAIT 7%   NET   RX 12 MB/s   TX 4 MB/s
quit refresh live staging tab next pause ? help                        sample 0.4s ago
```

---

## Installing

### apt (recommended for servers)

Once the apt repository is published (see *Publishing* below), every server is
two commands away:

```bash
curl -fsSL https://raw.githubusercontent.com/ahmedsajidh/otop/main/docs/install.sh | sudo sh
sudo apt install otop
```

and from then on `sudo apt upgrade` keeps it current. The first command only
installs the signing key and `/etc/apt/sources.list.d/otop.sources`; it is the
same one-time step a PPA needs. To do it by hand instead:

```bash
sudo curl -fsSL https://raw.githubusercontent.com/ahmedsajidh/otop/main/docs/otop-archive-keyring.gpg \
     -o /usr/share/keyrings/otop-archive-keyring.gpg
sudo curl -fsSL https://raw.githubusercontent.com/ahmedsajidh/otop/main/docs/otop.sources \
     -o /etc/apt/sources.list.d/otop.sources
sudo apt update && sudo apt install otop
```

### Debian / Ubuntu package file

```bash
./packaging/build-deb.sh              # produces otop_1.0.0_all.deb
sudo apt install ./otop_1.0.0_all.deb
sudo $EDITOR /etc/otop/config.yaml
otop
```

No virtualenv to activate: the package installs `/usr/bin/otop` and depends on
the distribution's `python3-psutil` and `python3-yaml` (plus
`python3-psycopg2`, recommended, for the PostgreSQL panel).

`build-deb.sh --arch amd64` produces `otop_1.0.0_amd64.deb` if a
machine-specific filename is wanted. The default is `all` because otop is pure
Python and an `Architecture: all` package installs on amd64, arm64 and anything
else.

### From source (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .
cp config/otop.yaml ~/.config/otop/config.yaml     # then edit it
.venv/bin/otop
```

`.venv/` is development-only and is not committed. otop never uses the Odoo
installation's virtualenv or its dependencies.

### Requirements

| | |
|---|---|
| Linux | worker detection reads `/proc` |
| Python | 3.8+ |
| `psutil` | required (`python3-psutil`) |
| `PyYAML` | required (`python3-yaml`) |
| `psycopg2` or `psycopg` 3 | optional (`python3-psycopg2`), enables the PostgreSQL panel |

---

## Configuration

`/etc/otop/config.yaml` (installed by the package; `~/.config/otop/config.yaml`
and `$OTOP_CONFIG` and `--config PATH` also work, in that order of precedence).

```yaml
refresh:
  fast: 2          # CPU, RAM, workers, disk I/O, network, pg_stat_activity
  slow: 30         # database size
  filestore: 300   # filestore directory walk
  tables: 900      # largest tables / index sizes
  discovery: 10    # full process-table rescan

disk_path: /
long_query_seconds: 5
show_query_text: true

instances:
  live:
    name: LIVE
    odoo_conf: /etc/odoo/odoo-live.conf
  staging:
    name: STAGING
    odoo_conf: /etc/odoo/odoo-staging.conf
```

Pointing at each instance's own `odoo.conf` is all that is normally needed —
otop reads from it:

| odoo.conf | used for |
|---|---|
| `db_name` | which database to query (first entry if it is a list) |
| `data_dir` | filestore path = `<data_dir>/filestore/<db_name>` |
| `db_host`, `db_port`, `db_user`, `db_password` | PostgreSQL connection |
| `http_port` | busy-worker detection and queue depth |
| `workers`, `max_cron_threads` | expected worker counts |

Every one of those can be overridden per instance (`database:`, `filestore:`,
`http_port:`, `process_match:`, `db: {host, port, user, password}`), and an
instance can be configured entirely by hand with no `odoo_conf` at all. Nothing
is hard-coded in otop; credentials stay in the configuration files and are never
displayed.

`process_match` is matched against the process command line **as it was
actually started**, and it defaults to the `odoo_conf` path. A service started
with a relative path (`odoo-bin -c odoo.conf` from the Odoo directory, as
`odoo.service` and many dev setups do) will therefore not match an absolute
`odoo_conf:` — set `process_match: odoo/odoo-bin`, or whatever substring is
unique to that instance's command line. If otop says `not running` for an
instance that is clearly up, this is why; compare with
`ps -eo pid,args | grep odoo`.

### PostgreSQL permissions

otop only reads: `pg_stat_activity`, `pg_database_size()` and
`pg_total_relation_size()`/`pg_indexes_size()`. The Odoo role itself is usually
enough. PostgreSQL hides `state`, `query` and `wait_event` for backends owned by
*other* roles — when otop detects that, it says so rather than reporting
misleadingly low numbers. Fix it with:

```sql
GRANT pg_monitor TO <the role otop connects as>;
```

---

## Using it

```bash
otop                          # first configured instance
otop -i staging               # start on STAGING
otop -n 1                     # refresh cheap metrics every second
otop --once                   # one plain-text snapshot, no curses (ssh, cron)
otop --config ./my.yaml
```

| key | action |
|---|---|
| `q` | quit |
| `r` | refresh now, including the cached filestore and database sizes |
| `l` / `s` | switch to LIVE / STAGING |
| `1`…`9` | switch to instance by position |
| `Tab` | next instance |
| `p` | pause / resume sampling |
| `?` | help, including what every marker means |

The layout adapts to the terminal: sections shrink (fewer worker rows, fewer
tables) before anything is dropped, and the footer is always visible.

---

## What the numbers mean

**CPU / LOAD** — CPU is the average across cores over the last interval; the
`cores` strip is one character per core (`' '` idle, then `.:-=+*#%@`). LOAD is
the 1/5/15 minute load average, coloured against the core count.

**RAM** — used is `total - available`, so page cache does not count as used.
**SWAP** — sustained swap use on an Odoo box is a red flag.

**ODOO WORKERS** — see the next section.

**STORAGE** — total disk usage of `disk_path` first, then the breakdown:
database (`pg_database_size`, refreshed every 30 s), filestore (the real
directory size, walked at most every 5 minutes, with the age of the figure
shown), and *other* = `disk used − (databases + filestores)`. Anything sitting
on a different filesystem is still listed but excluded from that subtraction and
labelled.

**POSTGRESQL** — connections on this instance's database (otop's own connection
excluded); `ACTIVE` are executing a statement, `IDLE-TX` are holding an open
transaction with nothing running (a growing number is a problem), `WAITING` are
active backends blocked on a lock, `LONGEST` is the age of the oldest running
statement. Long-running statements are listed with their text collapsed to one
line and truncated; `show_query_text: false` hides the text entirely.

**DISK / NET** — byte and operation rates from counter deltas, system-wide.

---

## Worker accuracy — what otop knows and what it cannot

Odoo's prefork server (`odoo/service/server.py`) works like this: a **master**
process binds the HTTP socket and forks children; **HTTP workers** accept a
connection from that shared socket and handle it synchronously, one request at a
time; **cron workers** never touch the HTTP socket and instead hold a connection
to the `postgres` maintenance database for `LISTEN cron_trigger`; the
**websocket / long-polling** process is spawned as `odoo-bin gevent`.

**Finding the processes.** The master is the process whose command line contains
`process_match` (the `odoo_conf` path by default) and whose parent is not
itself such a process; the rest of the tree is its children. That is also what
separates LIVE from STAGING. Only processes where Odoo is the program being
executed count, so `grep odoo-bin` or an editor with `odoo.conf` open is not
mistaken for Odoo.

**Worker types.** If the optional `setproctitle` package is installed in the
Odoo virtualenv, Odoo renames its children to `odoo: WorkerHTTP <pid>` /
`odoo: WorkerCron <pid> <db>` and the type is exact — installing it
(`pip install setproctitle`, then restart Odoo) is the single best thing you can
do for otop. Without it, every child shares the master's command line, and otop
falls back to: the gevent process by its argument; cron workers by their
connection to the `postgres` database; and whatever remains by spawn order
against `workers` / `max_cron_threads`. Anything inferred is marked `~`.

**Busy vs idle.** There is no Odoo API that reports "this worker is serving a
request", so otop uses the strongest evidence a Linux process can give:

* a worker that **owns an ESTABLISHED socket on the Odoo HTTP port** is inside a
  request — in prefork mode the accepted socket belongs to the worker only while
  it is handling that request (the prefork handler speaks HTTP/1.0, so no
  keep-alive socket lingers, and websockets live in the gevent process);
* a worker with an **active PostgreSQL backend** is working (this is how a cron
  worker running a job is detected);
* otherwise it is idle.

CPU usage is *not* used for this while sockets are readable. A worker at 80 % CPU
with no connection is reported IDLE, and a worker at 1 % CPU holding a request is
reported BUSY — both of which happen in practice.

Limits, stated plainly:

* **This is a snapshot, not a duty cycle.** Sampling every two seconds cannot see
  a 20 ms request, so a worker serving thousands of short requests is often
  caught idle. That is what the **BUSY%** column and the **AVG** counter are for:
  the share of the last ~30 samples in which the worker was busy. Judge
  saturation with those, not with the instantaneous BUSY count.
* **QUEUED** is the kernel's accept queue on the HTTP port — connections that
  have arrived and that no worker has picked up. Anything above 0 for more than a
  moment means every worker is busy. This one is exact.
* **Permissions matter.** Reading `/proc/<pid>/fd` requires running as root or as
  the Odoo user. Without it otop cannot see sockets and falls back to a CPU
  threshold (`busy_cpu_threshold`), marks every such status with `~`, and says so
  in a note. High CPU is never silently presented as "busy".
* If Odoo talks to PostgreSQL over a **unix socket**, backends cannot be matched
  to worker processes, so the per-worker DB column and cron identification are
  unavailable; the PostgreSQL panel itself still works.
* **Threaded mode** (`workers = 0`) has no worker processes at all. otop then
  reports requests *in flight* (established connections on the HTTP port) and the
  thread count, and says that per-request thread detail is not available.
* True request-level status (current route, request duration, queue time) would
  need instrumentation inside Odoo — for example a small addon exposing a status
  endpoint. It is deliberately **not** faked here.

---

## Overhead

otop samples in one background thread; the interface never blocks on a slow
database or a large filestore. The full process table (every `/proc/<pid>/cmdline`)
is rebuilt only every `discovery` seconds, and in between a single read of
`/proc/<master>/task/<master>/children` catches respawned workers immediately.
Per worker, each sample reads a handful of small `/proc` files. Sockets come from
one read of `/proc/net/tcp` plus the file descriptors of the Odoo workers only.
Filestore walks happen at most every `filestore` seconds in their own thread.

Measured on a 4-core laptop, interface running at the default 2 s refresh and
monitoring two Odoo instances (one with 5 worker processes): **1.2 % of one core
and 47 MB RSS** over a 25 second window.

## Error handling

otop keeps running and shows `N/A` or a short reason when:

| situation | shown |
|---|---|
| Odoo not running | `not running`, instance tab marked `!` |
| Odoo restarted, worker killed | new pids within one refresh |
| PostgreSQL down / wrong credentials | the error in the PostgreSQL panel; retried every 15 s |
| database missing | same |
| filestore path missing | `not found: …` in the storage row |
| `odoo.conf` unreadable | a note under the worker panel |
| `/proc/<pid>/fd` not readable | status falls back to `~` approximation, with a note |
| psycopg not installed | PostgreSQL panel disabled, everything else works |
| terminal too small | a message instead of a broken layout |

---

## Publishing the apt repository

`packaging/apt-repo.sh` turns the built `.deb` into a signed APT repository of
static files — no reprepro, aptly or apt-utils needed, just `dpkg-dev` and
`gpg`. It writes to `docs/`, which GitHub Pages serves directly.

```bash
./packaging/build-deb.sh                     # 1. build the package
./packaging/apt-repo.sh --generate-key       # 2. first time only: create the
                                             #    signing key, then build docs/
git add docs && git commit -m "apt repository for otop 1.0.0" && git push
```

`docs/` is then served straight from the repository over
`raw.githubusercontent.com`, which needs no Pages configuration at all — the
URL is baked into the generated `otop.sources` and `install.sh`.

GitHub Pages also works, *if* the account has no user-level custom domain: a
custom domain on the `<user>.github.io` repository is applied to every project
page, so `<user>.github.io/otop/` redirects to that domain, and unless it is
served by Pages too the repository becomes unreachable. To use a domain you
control, set it on **this** repository (Settings → Pages → Custom domain, e.g.
`apt.example.com`, with a CNAME to `<user>.github.io`), then rebuild with
`./packaging/apt-repo.sh --url https://apt.example.com`.

For later versions, bump `__version__` in `src/otop/__init__.py`, add a
`packaging/changelog.Debian` entry, then:

```bash
./packaging/build-deb.sh && ./packaging/apt-repo.sh
git add docs && git commit -m "otop X.Y.Z" && git push
```

Servers pick it up with `sudo apt update && sudo apt upgrade`.

Useful flags: `--sign <KEYID>` to use an existing key, `--url` / `--output` to
publish somewhere other than GitHub Pages (any nginx or S3 path works), and
`--unsigned` to skip GPG entirely — that emits a `Trusted: yes` source, which
means apt will **not** verify the repository's integrity, so only use it inside
a network you already trust.

Only one build per package version is published; if both the `all` and `amd64`
`.deb` are present the portable `all` one wins, so `apt-cache policy` stays
clean.

This path is verified end to end: a real `apt` run against a generated
repository fetches and validates `InRelease`, resolves `apt-cache policy otop`
to the published version, downloads the package — and rejects a tampered
`.deb`, a tampered index, and a repository signed by an untrusted key.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=src .venv/bin/python -m pytest tests -q
PYTHONPATH=src .venv/bin/python -m otop --config config/otop.yaml --once
```

The tests cover configuration and `odoo.conf` derivation, `/proc/net/tcp`
parsing, worker classification and the busy/idle decision matrix, the storage
maths and cache behaviour, the PostgreSQL aggregation, and the UI layout
(including a stopped Odoo, a dead database and tiny terminals). They need
neither Odoo nor PostgreSQL.

```
otop/
├── src/otop/
│   ├── main.py        CLI, sampling thread, curses loop
│   ├── ui.py          layout (built as data, then painted) and colours
│   ├── system.py      CPU, RAM, swap, load, disk I/O, network
│   ├── workers.py     Odoo process discovery and worker status (/proc)
│   ├── postgres.py    pg_stat_activity and sizes
│   ├── storage.py     disk usage, cached filestore walk, breakdown
│   ├── config.py      config.yaml + odoo.conf
│   └── format.py      human-readable numbers, gauges
├── tests/
├── packaging/
│   ├── build-deb.sh   dpkg-deb build (no debhelper needed) -- the tested path
│   ├── apt-repo.sh    builds the signed apt repository into docs/
│   ├── control.in, copyright, changelog.Debian, otop.1
│   └── debian/        dpkg-buildpackage/debhelper recipe (needs debhelper, dh-python)
├── config/otop.yaml
└── pyproject.toml
```

## Licence

MIT — see `LICENSE`.
