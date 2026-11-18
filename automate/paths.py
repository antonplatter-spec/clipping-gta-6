"""Where automate keeps its files.

Every location except ROOT is a function, resolved on each call, so a test can point the env vars below at a temp
dir per test case (the store reopens its connection when `db_path()` changes).

    ROOT                    the repo checkout (the clipper's ROOT, computed here so the server never imports clipper)
    data_dir()              $AUTOMATE_DATA_DIR or ROOT/data: the DB, the runtime overlay and scratch files (mode 0700)
    db_path()               data_dir()/automate.db (mode 0600)
    runtime_dir()           data_dir()/runtime: the YAML overlay the clipper subprocesses read (see runtime.materialize)
    runtime_profiles_dir()  runtime_dir()/profiles             -> CLIPPER_PROFILES_DIR
    runtime_watchlist()     runtime_dir()/watchlist.yaml       -> CLIPPER_CONFIG
    tmp_dir()               data_dir()/tmp: scratch for the agent loop and one-off picks files
    logs_dir()              $AUTOMATE_LOGS_DIR or ROOT/logs/automate
    jobs_dir()              $AUTOMATE_JOBS_DIR or ROOT/jobs
    exports_dir()           $AUTOMATE_EXPORTS_DIR or ROOT/exports
    repo_profiles_dir()     ROOT/profiles (tracked; read-only at runtime)
    repo_watchlist()        ROOT/watchlist.yaml (tracked; read-only at runtime)
    music_dir()             $AUTOMATE_MUSIC_DIR or ROOT/assets/music; music_local_dir() = its gitignored local/
                            folder (what the API serves sounds from; the bridge's clipper.music uses its own MUSIC)
    research_dir()          ROOT/research

AUTOMATE_JOBS_DIR / AUTOMATE_EXPORTS_DIR / AUTOMATE_MUSIC_DIR exist for tests of automate's own code: the clipper
subprocesses always use ROOT/jobs, ROOT/exports and ROOT/assets/music, so never set them on a real install.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_dir(var, default):
    value = os.environ.get(var)
    return Path(value).expanduser().resolve() if value else default


def data_dir():
    return _env_dir("AUTOMATE_DATA_DIR", ROOT / "data")


def db_path():
    return data_dir() / "automate.db"


def runtime_dir():
    return data_dir() / "runtime"


def runtime_profiles_dir():
    return runtime_dir() / "profiles"


def runtime_watchlist():
    return runtime_dir() / "watchlist.yaml"


def tmp_dir():
    return data_dir() / "tmp"


def logs_dir():
    return _env_dir("AUTOMATE_LOGS_DIR", ROOT / "logs" / "automate")


def jobs_dir():
    return _env_dir("AUTOMATE_JOBS_DIR", ROOT / "jobs")


def exports_dir():
    return _env_dir("AUTOMATE_EXPORTS_DIR", ROOT / "exports")


def repo_profiles_dir():
    return ROOT / "profiles"


def repo_watchlist():
    return ROOT / "watchlist.yaml"


def music_dir():
    return _env_dir("AUTOMATE_MUSIC_DIR", ROOT / "assets" / "music")


def music_local_dir():
    return music_dir() / "local"


def research_dir():
    return ROOT / "research"


def ensure_private_dir(path):
    """Create `path` (and parents) and make it owner-only: the data dir holds the DB with the secrets."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def ensure_data_dir():
    """data_dir() with runtime/ and tmp/ inside, all mode 0700."""
    d = ensure_private_dir(data_dir())
    ensure_private_dir(d / "runtime")
    ensure_private_dir(d / "tmp")
    return d


def rel(path):
    """`path` relative to ROOT as a posix string when it lies inside ROOT (how the DB stores export folders and
    files), else the absolute path."""
    p = Path(path)
    p = p if p.is_absolute() else ROOT / p
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p.resolve())


def from_rel(path):
    """The absolute path of a `rel()` string."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p
