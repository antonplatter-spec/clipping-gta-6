"""Worker threads: claim queued jobs and run them through pipeline.run_job.

Lanes (store.claim_next_job by kind, so a 4-hour download never blocks a render or a live chunk):
    source-1..N   source jobs (fetch, analyze, select, gate); N = setting worker.concurrency, followed live
    live          live_chunk jobs: a ready chunk goes straight to ③–④
    render        render and dub jobs, one at a time (they number the `video N` folders)
    upload        YouTube uploads
A supervisor thread heartbeats every running job (its lock), aborts a run whose lock is gone (canceled through the
API, or released), releases the locks of workers that died (pipeline.release_stale: counted as an attempt),
settles parents a crash left waiting (pipeline.reconcile, every minute and at start), restarts dead lane threads
and follows worker.concurrency. On `stop` every running command is stopped and its job goes back to the queue at
its stage (the next start continues there; an interrupted Claude step counts as an attempt).

Everything a stage does goes through a `Run` (one claimed job): subprocesses and in-process calls (Claude, YouTube)
run in helper threads while the worker thread pumps their output into job_events (throttled and bounded: a 4-hour
download prints thousands of progress lines) and `jobs.progress` (one short German line), and enforces timeouts
(overall, and "no output for so long" = hung). Writes that finish a stage happen in `run.owned()`: one transaction
that first checks this worker still holds the job, so a cancel that won is never overwritten.

    start_workers(stop) -> [threads]       request_cancel(job_id) -> bool       wake()
    process_next(worker_id, kinds=None, *, stop=None, now=None) -> job id | None   claim + run one job in this thread
    reap_orphans(module, args) -> [pids]   stop leftovers of an earlier server run executing the same command
                                           (Run.reap(args) does it for a stage before it adopts that command's output)
    active_jobs() -> [job ids]
"""
import collections
import itertools
import logging
import os
import re
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import db, pipeline, runtime, store

log = logging.getLogger("automate.worker")

POLL_S = 3.0               # idle lanes look at the queue this often (wake() cuts it short)
PUMP_S = 0.5
HEARTBEAT_S = 15.0
STALE_S = 300.0            # a running job whose lock wasn't refreshed this long belongs to a dead worker
RELEASE_EVERY_S = 60.0
ABANDON_S = 60.0           # after a stop request, how long a helper thread may take before it is left behind
PROGRESS_EVERY_S = 3.0
NOISE_EVERY_S = 60.0       # progress-bar lines: at most one event per minute
MAX_EVENTS = 400           # output lines stored per claimed run ...
MAX_ERROR_EVENTS = 100     # ... plus this many error-looking lines after that
MAX_EVENT_CHARS = 1000
NOISE_RE = re.compile(r"^\[download\]\s+[\d.]+%|^frame=|\btime=\d+:\d\d:\d\d|^\s*\d+%\|| \d+(\.\d+)?%\s*$")
# a command's JSON result (indented, one value per line): stored as data, not as events
JSON_LINE_RE = re.compile(r'^\s*([\[\]{}],?|"(?:[^"\\]|\\.)*"\s*(:.*|,?)|-?\d[\d.eE+-]*,?|true,?|false,?|null,?)\s*$')
ERRORISH_RE = re.compile(r"error|exception|traceback|failed|warning|refused|denied|not found", re.I)

_active = {}
_active_lock = threading.Lock()
_lanes = {}
_lanes_lock = threading.Lock()
_wake_event = threading.Event()
_seq = itertools.count(1)


def wake():
    """Something was queued: idle lanes look at the queue now."""
    _wake_event.set()


def active_jobs():
    with _active_lock:
        return sorted(_active)


def request_cancel(job_id):
    """Stop the command a worker runs for this job (the API changed its status first)."""
    with _active_lock:
        run = _active.get(job_id)
    if run is None:
        return False
    run.abort("cancel")
    return True


# ---------------------------------------------------------------- orphans

def _matches(argv, module, args):
    for i in range(len(argv) - 1):
        if argv[i] == "-m" and argv[i + 1] == module:
            tail = argv[i + 2:]
            return tail[:len(args)] == list(args)
    return False


def _processes():
    """[(pid, pgid, argv)] of every process (Linux /proc, else ps)."""
    out = []
    proc = Path("/proc")
    if proc.is_dir():
        for d in proc.glob("[0-9]*"):
            try:
                argv = [a.decode("utf-8", "replace") for a in (d / "cmdline").read_bytes().split(b"\0") if a]
                pid = int(d.name)
                out.append((pid, os.getpgid(pid), argv))
            except (OSError, ValueError):
                continue
        return out
    try:
        text = subprocess.run(["ps", "-ww", "-eo", "pid=,pgid=,args="], capture_output=True, text=True,
                              timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out.append((int(parts[0]), int(parts[1]), parts[2].split()))
    return out


def reap_orphans(module, args):
    """A server killed hard (power loss aside: SIGKILL, OOM) leaves the command of a running stage alive in its own
    process group. Before a stage runs the same command again, those leftovers are stopped, so two processes never
    write the same job folder. Detached live recorders (`live record`, `live analyze`) never match a stage's
    command."""
    args = [str(a) for a in args]
    me = os.getpid()
    victims = [(pid, pgid) for pid, pgid, argv in _processes() if pid != me and _matches(argv, module, args)]
    for pid, pgid in victims:
        try:
            if pgid == pid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    for pid, pgid in victims:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            try:
                (os.killpg if pgid == pid else os.kill)(pid, signal.SIGKILL)
            except OSError:
                pass
    return [pid for pid, _ in victims]


# ---------------------------------------------------------------- one claimed job

class Run:
    """One claimed job as its stages see it (see the module doc)."""

    def __init__(self, job, worker_id, stop=None):
        self.job_id, self.kind, self.profile = job["id"], job["kind"], job["profile"]
        self.worker_id = str(worker_id)
        self.stop = stop
        self.cancel = threading.Event()
        self.reason = None
        self.tail = collections.deque(maxlen=50)
        self._lines = collections.deque()
        self._last_progress = job.get("progress")
        self._progress_at = 0.0
        self._pending_progress = None
        self._events = 0
        self._dropped = 0
        self._noise_at = 0.0
        self._line_level = "debug"  # raw command output; the in-process calls' German status lines are "info"

    # -- state
    def abort(self, reason):
        if not self.cancel.is_set():
            self.reason = reason
        self.cancel.set()

    def check(self):
        if self.stop is not None and self.stop.is_set():
            self.abort("shutdown")
        if self.cancel.is_set():
            if self.reason in ("timeout", "stall"):
                raise pipeline.StageError("Zeitüberschreitung: der Schritt wurde abgebrochen", retryable=True)
            raise pipeline.Canceled(self.reason or "cancel")

    @property
    def job(self):
        return store.get_job(self.job_id)

    @contextmanager
    def owned(self):
        """One transaction that only goes ahead while this worker still holds the job."""
        with store.transaction():
            if not store.heartbeat_job(self.job_id, self.worker_id):
                raise pipeline.JobLost(self.job_id)
            yield

    def update(self, **fields):
        with self.owned():
            return store.update_job(self.job_id, **fields)

    # -- progress and events
    def progress(self, text, force=False):
        if not text or text == self._last_progress:
            return
        if not force and time.monotonic() - self._progress_at < PROGRESS_EVERY_S:
            self._pending_progress = text
            return
        self._write_progress(text)

    def _write_progress(self, text):
        self._pending_progress = None
        try:
            self.update(progress=str(text)[:200])
        except pipeline.JobLost:
            self.abort("lost")
            return
        self._last_progress, self._progress_at = text, time.monotonic()

    def event(self, message, level="info"):
        store.add_job_event(self.job_id, str(message)[:MAX_EVENT_CHARS], level)

    def log(self, line):
        """Thread-safe sink for output lines (subprocess readers, the agent's log callback)."""
        self._lines.append(("line", str(line)))

    def on_progress(self, done, total=None):
        """youtube.upload_variant's on_progress(done_bytes, total_bytes)."""
        self._lines.append(("bytes", (done, total)))

    def _drain(self, parse=None):
        got = False
        while self._lines:
            kind, item = self._lines.popleft()
            got = True
            if kind == "bytes":
                done, total = item
                if total:
                    self.progress(f"Lade auf YouTube hoch … {100 * float(done) / float(total):.0f} %")
                continue
            line = item.rstrip()
            if not line.strip():
                continue
            self.tail.append(line)
            text = parse(line) if parse else None
            if text:
                self.progress(text)
            # only progress-bar output is throttled; a line a parser turned into a status ("Claude liest das
            # Transkript", "[render] video 1/…mp4") is part of the job's story and kept
            self._store_line(line, noisy=bool(NOISE_RE.search(line)))
        if self._pending_progress and time.monotonic() - self._progress_at >= PROGRESS_EVERY_S:
            self._write_progress(self._pending_progress)
        return got

    def _store_line(self, line, noisy):
        if JSON_LINE_RE.match(line):
            self._dropped += 1
            return
        errorish = bool(ERRORISH_RE.search(line))
        if noisy and not errorish:
            if time.monotonic() - self._noise_at < NOISE_EVERY_S:
                self._dropped += 1
                return
            self._noise_at = time.monotonic()
        if self._events >= MAX_EVENTS + MAX_ERROR_EVENTS or (self._events >= MAX_EVENTS and not errorish):
            self._dropped += 1
            return
        self._events += 1
        self.event(line, "warning" if errorish else self._line_level)

    def _flush_dropped(self):
        if self._dropped:
            self.event(f"… {self._dropped} weitere Ausgabezeilen nicht gespeichert (gedrosselt)", "debug")
            self._dropped = 0

    # -- running things
    def _wait(self, thread, parse=None, stall=None, deadline=None):
        """Pump output while `thread` runs; enforce stall/deadline/stop. False when the thread was left behind."""
        last, grace = time.monotonic(), None
        while thread.is_alive():
            thread.join(PUMP_S)
            if self._drain(parse):
                last = time.monotonic()
            now = time.monotonic()
            if not self.cancel.is_set():
                if self.stop is not None and self.stop.is_set():
                    self.abort("shutdown")
                elif stall and now - last > stall:
                    self.event(f"Keine Ausgabe seit {stall / 60:.0f} Min.: der Befehl hängt und wird beendet",
                               "warning")
                    self.abort("stall")
                elif deadline and now > deadline:
                    self.abort("timeout")
            if self.cancel.is_set():
                grace = grace or now + ABANDON_S
                if now > grace:
                    log.warning("job %s: helper thread %s did not stop; left behind", self.job_id, thread.name)
                    return False
        self._drain(parse)
        return True

    def _thread(self, name, fn):
        box = {}

        def target():
            try:
                box["r"] = fn()
            except BaseException as e:  # handed to the worker thread
                box["e"] = e
            finally:
                db.close()
        t = threading.Thread(target=target, name=f"job-{self.job_id}-{name}", daemon=True)
        t.start()
        return t, box

    def reap(self, args, module="clipper"):
        """Stop what an earlier server run left running for this job (`python -m <module> <args…>`): before a stage
        looks at that command's half-written output, nothing may still be writing it."""
        killed = reap_orphans(module, [str(a) for a in args])
        if killed:
            self.event(f"Übrig gebliebene Prozesse eines früheren Laufs beendet (PID {', '.join(map(str, killed))})",
                       "warning")
        return killed

    def clipper(self, args, *, timeout, stall=None, progress=None, module="clipper", input=None):
        """`python -m clipper <args>` for this job's profile -> ProcResult (any exit code). Canceled -> Canceled;
        timed out or hung -> StageError (retryable)."""
        self.check()
        args = [str(a) for a in args]
        self.reap(args, module)
        t, box = self._thread(args[0], lambda: runtime.run_clipper(
            args, self.profile, timeout=timeout, on_line=self.log, cancel=self.cancel, module=module, input=input))
        finished = self._wait(t, parse=progress, stall=stall)
        self._flush_dropped()
        what = " ".join(args[:2])
        if not finished:
            raise pipeline.Canceled(self.reason or "cancel")
        if "e" in box:
            raise box["e"]
        res = box["r"]
        if res.canceled:
            if self.reason in ("stall", "timeout"):
                raise pipeline.StageError(f"„clipper {what}“ gab zu lange keine Ausgabe und wurde beendet",
                                          retryable=True)
            raise pipeline.Canceled(self.reason or "cancel")
        if res.timed_out:
            raise pipeline.StageError(f"Zeitüberschreitung: „clipper {what}“ lief länger als {timeout / 60:.0f} Min. "
                                      "und wurde beendet", retryable=True)
        if res.returncode != 0:
            self.event(f"„clipper {what}“ endete mit Code {res.returncode}", "warning")
        return res

    def bridge(self, op, payload, *, timeout):
        """runtime.run_bridge for this job's profile; errors become StageError (retryable)."""
        self.check()
        t, box = self._thread(op, lambda: runtime.run_bridge(op, payload, self.profile, timeout=timeout,
                                                             cancel=self.cancel))
        if not self._wait(t):
            raise pipeline.Canceled(self.reason or "cancel")
        e = box.get("e")
        if e is None:
            return box["r"]
        if isinstance(e, runtime.BridgeError):
            if e.canceled:
                if self.reason in ("stall", "timeout"):
                    raise pipeline.StageError(f"{op}: Zeitüberschreitung", retryable=True)
                raise pipeline.Canceled(self.reason or "cancel")
            if e.timed_out:
                raise pipeline.StageError(f"{op}: Zeitüberschreitung nach {timeout} s", retryable=True)
            tail = [x for x in (e.stderr or "").strip().splitlines() if x.strip()][-5:]
            if tail:
                self.event(f"{op}: " + " | ".join(tail), "warning")
            raise pipeline.StageError(f"{op}: {e.message}", retryable=True)
        raise e

    def call(self, fn, *args, timeout, progress=None, **kwargs):
        """An in-process call (Claude, YouTube) in a helper thread, with this run's timeout and stop handling; pass
        `log=run.log` / `on_progress=run.on_progress` / `cancel=run.cancel` yourself as the function takes them.
        When the call returns a result it is returned even if a cancel, stop or timeout arrived meanwhile."""
        self.check()
        t, box = self._thread(getattr(fn, "__name__", "call"), lambda: fn(*args, **kwargs))
        self._line_level = "info"
        try:
            finished = self._wait(t, parse=progress or (lambda line: f"Claude: {line[:100]}"),
                                  deadline=time.monotonic() + timeout)
        finally:
            self._line_level = "debug"
        self._flush_dropped()
        if finished and "r" in box:  # it completed: a finished upload or a paid selection is never thrown away
            return box["r"]  # because a stop arrived meanwhile; the stage's own (owned) write decides what stands
        if self.reason in ("cancel", "lost") or (not finished and self.reason == "shutdown"):
            raise pipeline.Canceled(self.reason)
        if not finished or (self.reason == "timeout" and "r" not in box):
            raise pipeline.StageError(f"Zeitüberschreitung: {getattr(fn, '__name__', 'Aufruf')} lief länger als "
                                      f"{timeout / 60:.0f} Min.", retryable=True)
        if "e" in box:
            if self.reason == "shutdown":
                raise pipeline.Canceled("shutdown")
            raise box["e"]
        return box["r"]


# ---------------------------------------------------------------- lanes

def process_next(worker_id, kinds=None, *, stop=None, now=None):
    """Claim the next job (of `kinds`) and run it to its next resting point in this thread."""
    job = store.claim_next_job(worker_id, kinds, now=now)
    if job is None:
        return None
    run = Run(job, worker_id, stop)
    with _active_lock:
        _active[job["id"]] = run
    try:
        pipeline.run_job(run)
    finally:
        with _active_lock:
            if _active.get(job["id"]) is run:
                del _active[job["id"]]
    return job["id"]


def _concurrency():
    try:
        return max(1, int(store.get_setting("worker.concurrency") or 1))
    except Exception:
        return 1


def _lane(name, kinds, index, stop):
    wid = f"{os.getpid()}:{name}:{next(_seq)}"
    try:
        while not stop.is_set():
            if index is not None and index >= _concurrency():
                return
            try:
                jid = process_next(wid, kinds, stop=stop)
            except Exception:
                log.exception("worker %s failed", name)
                jid = None
                stop.wait(5)
            if jid is None and not stop.is_set():
                _wake_event.wait(POLL_S)
                _wake_event.clear()
    finally:
        db.close()


def _ensure_lanes(stop):
    wanted = [("live", ("live_chunk",), None), ("render", ("render", "dub"), None), ("upload", ("upload",), None)]
    wanted += [(f"source-{i + 1}", ("source",), i) for i in range(_concurrency())]
    started = []
    with _lanes_lock:
        for name, kinds, index in wanted:
            t, owner = _lanes.get(name, (None, None))
            if t is not None and t.is_alive() and owner is stop:
                continue
            if stop.is_set():
                continue
            t = threading.Thread(target=_lane, args=(name, kinds, index, stop), name=f"worker-{name}", daemon=True)
            _lanes[name] = (t, stop)
            t.start()
            started.append(t)
    return started


def _supervisor(stop):
    last_release = 0.0
    try:
        while not stop.wait(HEARTBEAT_S):
            with _active_lock:
                runs = list(_active.values())
            for run in runs:
                try:
                    if not store.heartbeat_job(run.job_id, run.worker_id):
                        run.abort("lost")
                except Exception:
                    log.exception("heartbeat of job %s failed", run.job_id)
            if time.monotonic() - last_release >= RELEASE_EVERY_S:
                last_release = time.monotonic()
                try:
                    ids = pipeline.release_stale(STALE_S)
                    if ids:
                        log.warning("released stale jobs %s", ids)
                        wake()
                except Exception:
                    log.exception("release_stale failed")
                try:
                    pipeline.reconcile()
                except Exception:
                    log.exception("reconcile failed")
            try:
                _ensure_lanes(stop)
            except Exception:
                log.exception("restarting worker lanes failed")
    finally:
        with _active_lock:
            runs = list(_active.values())
        for run in runs:
            run.abort("shutdown")
        wake()
        db.close()


def start_workers(stop):
    """Start the lanes and their supervisor (daemon threads, all ending soon after `stop` is set)."""
    pipeline.release_stale(0)
    try:
        pipeline.reconcile()
    except Exception:
        log.exception("reconcile at startup failed")
    threads = _ensure_lanes(stop)
    sup = threading.Thread(target=_supervisor, args=(stop,), name="worker-supervisor", daemon=True)
    sup.start()
    return [sup, *threads]
