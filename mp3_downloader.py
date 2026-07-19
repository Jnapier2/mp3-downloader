#!/usr/bin/env python3
"""Guarded audio-only downloader for authorized public media.

Copyright © 2026 Gateway Information Group LLC. All rights reserved.
Third-party components retain their own copyright notices and licenses.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as importlib_metadata
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import random
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse, urlunparse
import zipfile

APP_ROOT = Path(__file__).resolve().parent
APP_NAME = "MP3 Downloader"
APP_VERSION = "1.0.0"
RELEASE_CHANNEL = "public"
RIGHTS_HOLDER = "Gateway Information Group LLC"
COPYRIGHT_NOTICE = "Copyright © 2026 Gateway Information Group LLC. All rights reserved."
THIRD_PARTY_POLICY = "Preserve all third-party/open-source notices and licenses; no ownership is claimed over upstream components."
SUPPORT_EXPORT_SCHEMA = 1
CONFIG_SCHEMA_VERSION = 1
MAX_PARALLEL_LINKS = 3
MAX_FRAGMENT_WORKERS = 5
LOCK_STALE_SECONDS = 6 * 60 * 60
LOCK_HEARTBEAT_SECONDS = 30
DUPLICATE_DB_TIMEOUT_SECONDS = 15
EXPECTED_RUNTIME_PINS: Dict[str, str] = {
    "certifi": "2026.6.17",
    "yt-dlp": "2026.7.4",
}
ALLOWED_RUNTIME_DISTRIBUTIONS = frozenset({*EXPECTED_RUNTIME_PINS, "pip", "setuptools", "wheel"})

DOWNLOADS_DIR = APP_ROOT / "downloads"
LOGS_DIR = APP_ROOT / "logs"
STATE_DIR = APP_ROOT / "state"
TEMP_DIR = APP_ROOT / "temp"
EXPORTS_DIR = APP_ROOT / "support_exports"
DEFAULT_CONFIG_PATH = APP_ROOT / "config.json"
LOCK_PATH = STATE_DIR / "mp3_downloader.lock"
RUN_HISTORY_PATH = STATE_DIR / "run_history.jsonl"
QUEUE_STATE_PATH = STATE_DIR / "link_queue_status.json"
HOST_TOLERANCE_PATH = STATE_DIR / "host_tolerance.json"
HOST_TOLERANCE_LOCK_PATH = STATE_DIR / "host_tolerance.lock"
DUPLICATE_INDEX_PATH = STATE_DIR / "download_index.sqlite3"

RUN_STARTED_MONOTONIC = time.monotonic()
RUN_STARTED_UTC = datetime.now(timezone.utc)
RUN_ID = f"{RUN_STARTED_UTC.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
TERMINAL_STATUS = "initializing"
LAST_MAJOR_STEP = "startup"
LAST_PROGRESS_UTC = RUN_STARTED_UTC
RUN_HISTORY_LOCK = threading.Lock()
HOST_TOLERANCE_LOCK = threading.Lock()

DEFAULT_CONFIG: Dict[str, Any] = {
    "config_version": CONFIG_SCHEMA_VERSION,
    "mp3_quality_kbps": 192,
    "timeout_seconds": 30,
    "retries": 5,
    "fragment_retries": 10,
    "outer_recovery_attempts": 1,
    "retry_backoff_seconds": 1.5,
    "retry_jitter_seconds": 0.75,
    "retry_after_cap_seconds": 120,
    "max_size_mb": 2048,
    "verify_ssl": True,
    "overwrite": False,
    "keep_partial_on_error": True,
    "single_instance_guard": True,
    "duplicate_detection_enabled": True,
    "hide_completed_media": False,
    "write_metadata_tags": True,
    "allow_live_streams": False,
    "allow_private_networks": False,
    "ffmpeg_location": "",
    "adaptive_fragment_workers": 3,
    "adaptive_fragment_workers_min": 1,
    "adaptive_fragment_workers_max": 5,
    "adaptive_fragment_burst_successes": 3,
    "queue_worker_idle_timeout_seconds": 600,
    "queue_worker_restart_on_stall": True,
    "dry_run": False,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 "
        "MP3Downloader/1.0.0"
    ),
    "output_filename_template": "%(title).180B [%(id)s].%(ext)s",
}

SENSITIVE_KEY_EXACT = frozenset({
    "token", "secret", "signature", "sig", "auth", "authorization", "password",
    "passwd", "pwd", "cookie", "cookies", "session", "credential", "credentials",
    "access_token", "refresh_token", "api_key", "private_key", "secret_key",
    "session_id", "session_key", "jwt", "bearer", "x_api_key",
})
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
EMAIL_PATTERN = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])")
BEARER_TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+[A-Za-z0-9._~+/=-]+"
)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[-_ ]?key|"
    r"access[-_ ]?token|refresh[-_ ]?token|token|password|passwd|secret|bearer)"
    r"\b\s*[:=]\s*([^\s,;]+)"
)

logger = logging.getLogger("mp3_downloader")


class DownloaderError(RuntimeError):
    """Expected user-facing downloader failure."""


class UnsupportedConfigError(DownloaderError):
    """The config is newer than this program or otherwise unsupported."""


def build_local_timezone():
    """Use the operating system's configured timezone without a fixed locale."""
    try:
        detected = datetime.now().astimezone().tzinfo
        if detected is not None:
            return detected
    except Exception:
        pass
    return timezone.utc


LOCAL_TZ = build_local_timezone()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: Optional[datetime] = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_zone_label(value: Optional[datetime] = None) -> str:
    dt = (value or utc_now()).astimezone(LOCAL_TZ)
    raw = str(dt.tzname() or "LOCAL").strip()
    compact = re.sub(r"[^A-Za-z0-9_-]+", "", raw).upper()
    return compact if 1 <= len(compact) <= 12 else "LOCAL"


def local_label(value: Optional[datetime] = None) -> str:
    dt = (value or utc_now()).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S ") + local_zone_label(dt)


def local_filename_stamp(value: Optional[datetime] = None) -> str:
    dt = (value or utc_now()).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d_%H%M%S_") + local_zone_label(dt)


def elapsed_seconds() -> float:
    return round(max(0.0, time.monotonic() - RUN_STARTED_MONOTONIC), 3)


def mark_progress(step: str) -> None:
    global LAST_MAJOR_STEP, LAST_PROGRESS_UTC
    LAST_MAJOR_STEP = step
    LAST_PROGRESS_UTC = utc_now()


def set_terminal_status(status: str) -> None:
    global TERMINAL_STATUS
    TERMINAL_STATUS = status
    mark_progress(status)


def ensure_dirs() -> None:
    for path in (DOWNLOADS_DIR, LOGS_DIR, STATE_DIR, TEMP_DIR, EXPORTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def is_sensitive_key(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not normalized:
        return False
    if normalized in SENSITIVE_KEY_EXACT:
        return True
    tokens = set(normalized.split("_"))
    if tokens.intersection({"password", "passwd", "secret", "cookie", "cookies", "credential", "credentials", "bearer", "jwt"}):
        return True
    return (
        normalized.endswith("_token")
        or normalized.startswith("token_")
        or "api_key" in normalized
        or "private_key" in normalized
        or "secret_key" in normalized
        or "auth_token" in normalized
        or normalized.endswith("_signature")
    )


def sanitize_untrusted_text(value: Any, max_length: int = 1000) -> str:
    """Collapse ANSI/control and bidirectional override characters from untrusted display/log text."""
    text = ANSI_ESCAPE_PATTERN.sub("", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", " ", text)
    return " ".join(text.split())[:max(0, int(max_length))]


def redact_url_value(url: str) -> str:
    """Replace a full URL with a host plus one-way digest descriptor."""
    raw = str(url or "")
    try:
        parsed = urlparse(raw)
        host = sanitize_untrusted_text(parsed.hostname or "unknown-host", 253).lower()
    except Exception:
        host = "unknown-host"
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"[URL_REDACTED host={host or 'unknown-host'} sha256={digest}]"


def redact_sensitive_text(text: str) -> str:
    if not text:
        return text
    cleaned = sanitize_untrusted_text(text, max(1000, len(str(text)) + 32))
    redacted = URL_PATTERN.sub(lambda match: redact_url_value(match.group(0)), cleaned)
    redacted = EMAIL_PATTERN.sub("[EMAIL_REDACTED]", redacted)
    redacted = BEARER_TOKEN_PATTERN.sub("[BEARER_TOKEN_REDACTED]", redacted)
    for local_root in (str(APP_ROOT), str(Path.home())):
        if local_root:
            redacted = re.sub(re.escape(local_root), "[LOCAL_PATH_REDACTED]", redacted, flags=re.IGNORECASE)
    return SENSITIVE_TEXT_PATTERN.sub(lambda match: f"{match.group(1)}=REDACTED", redacted)


def redact_for_export(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [redact_for_export(item) for item in value]
    if isinstance(value, tuple):
        return [redact_for_export(item) for item in value]
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text) and not isinstance(item, (bool, int, float, type(None))):
                cleaned[key_text] = "REDACTED"
            else:
                cleaned[key_text] = redact_for_export(item)
        return cleaned
    return value


def safe_relative_name(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(APP_ROOT.resolve()))
    except Exception:
        return path.name


def normalize_url_for_digest(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    return urlunparse((parsed.scheme.lower(), netloc, parsed.path or "/", "", parsed.query, ""))


def url_descriptor(url: str) -> Dict[str, str]:
    try:
        host = (urlparse(url).hostname or "unknown-host").lower()
    except Exception:
        host = "unknown-host"
    digest = hashlib.sha256(normalize_url_for_digest(url).encode("utf-8", errors="ignore")).hexdigest()
    return {"host": host, "url_sha256_prefix": digest[:16]}


def setup_logging(verbose: bool = False) -> None:
    ensure_dirs()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(message)s")
    file_handler = RotatingFileHandler(
        LOGS_DIR / "mp3_downloader.log",
        maxBytes=1_500_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


def append_run_history(event: str, details: Optional[Dict[str, Any]] = None) -> None:
    ensure_dirs()
    payload = {
        "utc": utc_iso(),
        "local": local_label(),
        "run_id": RUN_ID,
        "event": event,
        "terminal_status": TERMINAL_STATUS,
        "elapsed_seconds": elapsed_seconds(),
        "details": redact_for_export(details or {}),
    }
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with RUN_HISTORY_LOCK:
        try:
            with RUN_HISTORY_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            lines = RUN_HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > 300:
                temp_path = RUN_HISTORY_PATH.with_suffix(".jsonl.tmp")
                temp_path.write_text("\n".join(lines[-300:]) + "\n", encoding="utf-8")
                temp_path.replace(RUN_HISTORY_PATH)
        except Exception:
            pass


def coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def coerce_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_output_filename_template(value: Any) -> str:
    """Return a filename-only yt-dlp template that cannot escape the selected output folder."""
    default = str(DEFAULT_CONFIG["output_filename_template"])
    template = str(value or default).strip()[:240]
    if (
        not template
        or "\x00" in template
        or "/" in template
        or "\\" in template
        or template in {".", ".."}
        or ".." in template
        or re.match(r"^[A-Za-z]:", template)
        or "%(id" not in template.casefold()
        or "%(ext" not in template.casefold()
    ):
        return default
    return template


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def validate_output_filesystem_state(path: Path, out_dir: Path) -> Path:
    """Reject redirected or multiply linked output files and return a contained path."""
    try:
        metadata = os.lstat(path)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        file_attributes = int(getattr(metadata, "st_file_attributes", 0))
        if path.is_symlink() or (reparse_flag and file_attributes & reparse_flag):
            raise DownloaderError(
                "The final output is a symbolic link or reparse target. Remove it and retry."
            )
        if not stat.S_ISREG(metadata.st_mode):
            raise DownloaderError("The final output is not a regular file.")
        if int(getattr(metadata, "st_nlink", 1)) > 1:
            raise DownloaderError(
                "The final output has multiple hard links. Remove it and retry."
            )
        resolved = path.resolve(strict=True)
    except DownloaderError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise DownloaderError(f"The final output could not be inspected safely: {exc}") from exc
    if not path_is_within(resolved, out_dir):
        raise DownloaderError("The downloader refused an output path outside the selected output folder.")
    return resolved


def validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    raw_version = coerce_int(raw.get("config_version", 0), 0, 0, 9999)
    if raw_version > CONFIG_SCHEMA_VERSION:
        raise UnsupportedConfigError(
            f"config_version {raw_version} is newer than supported schema {CONFIG_SCHEMA_VERSION}."
        )
    normalized = dict(DEFAULT_CONFIG)
    normalized.update({
        "mp3_quality_kbps": coerce_int(raw.get("mp3_quality_kbps"), DEFAULT_CONFIG["mp3_quality_kbps"], 64, 320),
        "timeout_seconds": coerce_int(raw.get("timeout_seconds"), DEFAULT_CONFIG["timeout_seconds"], 10, 180),
        "retries": coerce_int(raw.get("retries"), DEFAULT_CONFIG["retries"], 0, 20),
        "fragment_retries": coerce_int(raw.get("fragment_retries"), DEFAULT_CONFIG["fragment_retries"], 0, 50),
        "outer_recovery_attempts": coerce_int(raw.get("outer_recovery_attempts"), DEFAULT_CONFIG["outer_recovery_attempts"], 0, 3),
        "retry_backoff_seconds": coerce_float(raw.get("retry_backoff_seconds"), DEFAULT_CONFIG["retry_backoff_seconds"], 0.0, 30.0),
        "retry_jitter_seconds": coerce_float(raw.get("retry_jitter_seconds"), DEFAULT_CONFIG["retry_jitter_seconds"], 0.0, 10.0),
        "retry_after_cap_seconds": coerce_float(raw.get("retry_after_cap_seconds"), DEFAULT_CONFIG["retry_after_cap_seconds"], 5.0, 600.0),
        "max_size_mb": coerce_float(raw.get("max_size_mb"), DEFAULT_CONFIG["max_size_mb"], 10.0, 10240.0),
        "verify_ssl": coerce_bool(raw.get("verify_ssl"), DEFAULT_CONFIG["verify_ssl"]),
        "overwrite": coerce_bool(raw.get("overwrite"), DEFAULT_CONFIG["overwrite"]),
        "keep_partial_on_error": coerce_bool(raw.get("keep_partial_on_error"), DEFAULT_CONFIG["keep_partial_on_error"]),
        "single_instance_guard": coerce_bool(raw.get("single_instance_guard"), DEFAULT_CONFIG["single_instance_guard"]),
        "duplicate_detection_enabled": coerce_bool(raw.get("duplicate_detection_enabled"), DEFAULT_CONFIG["duplicate_detection_enabled"]),
        "hide_completed_media": coerce_bool(raw.get("hide_completed_media"), DEFAULT_CONFIG["hide_completed_media"]),
        "write_metadata_tags": coerce_bool(raw.get("write_metadata_tags"), DEFAULT_CONFIG["write_metadata_tags"]),
        "allow_live_streams": coerce_bool(raw.get("allow_live_streams"), DEFAULT_CONFIG["allow_live_streams"]),
        "allow_private_networks": coerce_bool(raw.get("allow_private_networks"), DEFAULT_CONFIG["allow_private_networks"]),
        "ffmpeg_location": str(raw.get("ffmpeg_location") or DEFAULT_CONFIG["ffmpeg_location"]).strip(),
        "adaptive_fragment_workers": coerce_int(raw.get("adaptive_fragment_workers"), DEFAULT_CONFIG["adaptive_fragment_workers"], 1, MAX_FRAGMENT_WORKERS),
        "adaptive_fragment_workers_min": coerce_int(raw.get("adaptive_fragment_workers_min"), DEFAULT_CONFIG["adaptive_fragment_workers_min"], 1, MAX_FRAGMENT_WORKERS),
        "adaptive_fragment_workers_max": coerce_int(raw.get("adaptive_fragment_workers_max"), DEFAULT_CONFIG["adaptive_fragment_workers_max"], 1, MAX_FRAGMENT_WORKERS),
        "adaptive_fragment_burst_successes": coerce_int(raw.get("adaptive_fragment_burst_successes"), DEFAULT_CONFIG["adaptive_fragment_burst_successes"], 1, 20),
        "queue_worker_idle_timeout_seconds": coerce_int(raw.get("queue_worker_idle_timeout_seconds"), DEFAULT_CONFIG["queue_worker_idle_timeout_seconds"], 120, 7200),
        "queue_worker_restart_on_stall": coerce_bool(raw.get("queue_worker_restart_on_stall"), DEFAULT_CONFIG["queue_worker_restart_on_stall"]),
        "dry_run": coerce_bool(raw.get("dry_run"), DEFAULT_CONFIG["dry_run"]),
        "user_agent": str(raw.get("user_agent") or DEFAULT_CONFIG["user_agent"]).strip()[:500],
        "output_filename_template": normalize_output_filename_template(
            raw.get("output_filename_template")
        ),
    })
    normalized["adaptive_fragment_workers_min"] = min(
        normalized["adaptive_fragment_workers"], normalized["adaptive_fragment_workers_min"]
    )
    normalized["adaptive_fragment_workers_max"] = max(
        normalized["adaptive_fragment_workers"], normalized["adaptive_fragment_workers_max"]
    )
    normalized["config_version"] = CONFIG_SCHEMA_VERSION
    return normalized


def sync_config_file(config_path: Path) -> int:
    ensure_dirs()
    try:
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        else:
            raw = {}
        normalized = validate_config(raw)
        if config_path.exists():
            current_text = json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
            normalized_text = json.dumps(normalized, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
            if current_text != normalized_text:
                backup = config_path.with_name(
                    f"config_pre_sync_{local_filename_stamp()}.json"
                )
                shutil.copy2(config_path, backup)
        temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp_path.replace(config_path)
        print(f"[OK] Config synchronized: {config_path}")
        return 0
    except Exception as exc:
        print(f"[ERROR] Config synchronization failed: {redact_sensitive_text(str(exc))}")
        return 1


def load_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        if sync_config_file(config_path) != 0:
            raise DownloaderError("Could not create config.json")
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    return validate_config(raw)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


class InstanceGuard:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.acquired = False
        self.stop_event = threading.Event()
        self.heartbeat_thread: Optional[threading.Thread] = None

    def _metadata(self, event: str) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "pid": os.getpid(),
            "run_id": RUN_ID,
            "event": event,
            "updated_utc": utc_iso(),
            "project_root_sha256_prefix": hashlib.sha256(
                str(APP_ROOT.resolve()).lower().encode("utf-8", errors="ignore")
            ).hexdigest()[:16],
        }

    def _write_heartbeat(self) -> None:
        while not self.stop_event.wait(LOCK_HEARTBEAT_SECONDS):
            try:
                temp = LOCK_PATH.with_suffix(".lock.tmp")
                temp.write_text(json.dumps(self._metadata("heartbeat"), indent=2), encoding="utf-8")
                temp.replace(LOCK_PATH)
            except Exception:
                pass

    def acquire(self) -> bool:
        if not self.enabled:
            return True
        ensure_dirs()
        if LOCK_PATH.exists():
            try:
                payload = json.loads(LOCK_PATH.read_text(encoding="utf-8", errors="replace"))
                pid = int(payload.get("pid", 0) or 0)
                updated = LOCK_PATH.stat().st_mtime
                if process_is_alive(pid) and (time.time() - updated) < LOCK_STALE_SECONDS:
                    logger.error(f"Another {APP_NAME} session appears active (PID {pid}).")
                    return False
            except Exception:
                pass
            try:
                stale_path = LOCK_PATH.with_name(f"mp3_downloader_stale_{local_filename_stamp()}.lock")
                LOCK_PATH.replace(stale_path)
            except Exception:
                try:
                    LOCK_PATH.unlink(missing_ok=True)
                except Exception:
                    return False
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(str(LOCK_PATH), flags)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._metadata("acquired"), handle, indent=2)
            self.acquired = True
            self.heartbeat_thread = threading.Thread(target=self._write_heartbeat, daemon=True)
            self.heartbeat_thread.start()
            return True
        except FileExistsError:
            logger.error(f"Another {APP_NAME} session acquired the project lock first.")
            return False
        except Exception as exc:
            logger.error(f"Could not create the single-instance lock: {exc}")
            return False

    def release(self) -> None:
        if not self.enabled or not self.acquired:
            return
        self.stop_event.set()
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        self.acquired = False


def exact_pinned_requirements() -> Dict[str, str]:
    path = APP_ROOT / "requirements.txt"
    pins: Dict[str, str] = {}
    if not path.exists():
        return pins
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version_and_options = line.split("==", 1)
        version_parts = version_and_options.strip().split(maxsplit=1)
        if not version_parts:
            continue
        pins[name.strip().lower().replace("_", "-")] = version_parts[0]
    return pins


def canonical_distribution_name(value: Any) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "").strip()).lower()


def dependency_health() -> Dict[str, Any]:
    lock_pins = exact_pinned_requirements()
    mismatches: List[Dict[str, str]] = []
    installed: Dict[str, Optional[str]] = {}
    for name, required in EXPECTED_RUNTIME_PINS.items():
        lock_value = lock_pins.get(name)
        if lock_value != required:
            mismatches.append({
                "package": f"requirements.txt:{name}",
                "required": required,
                "installed": lock_value or "missing",
            })
        try:
            actual = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            actual = None
        installed[name] = actual
        if actual != required:
            mismatches.append({"package": name, "required": required, "installed": actual or "missing"})
    for name, value in sorted(lock_pins.items()):
        if name not in EXPECTED_RUNTIME_PINS:
            mismatches.append({
                "package": f"requirements.txt:{name}",
                "required": "not present",
                "installed": value,
            })
    discovered: Dict[str, str] = {}
    try:
        for distribution in importlib_metadata.distributions():
            raw_name = distribution.metadata.get("Name") or ""
            name = canonical_distribution_name(raw_name)
            if name:
                discovered[name] = str(distribution.version or "unknown")
    except Exception as exc:
        mismatches.append({
            "package": "environment-distribution-scan",
            "required": "successful",
            "installed": f"error:{exc.__class__.__name__}",
        })
    unexpected = {
        name: version for name, version in sorted(discovered.items())
        if name not in ALLOWED_RUNTIME_DISTRIBUTIONS
    }
    for name, version in unexpected.items():
        mismatches.append({"package": name, "required": "not installed", "installed": version})
    return {
        "status": "verified" if lock_pins == EXPECTED_RUNTIME_PINS and not mismatches else "repair_required",
        "pins": dict(EXPECTED_RUNTIME_PINS),
        "lock_file_pins": lock_pins,
        "installed": installed,
        "allowed_distribution_names": sorted(ALLOWED_RUNTIME_DISTRIBUTIONS),
        "unexpected_distributions": unexpected,
        "mismatches": mismatches,
    }


def support_dependency_snapshot() -> Dict[str, Any]:
    """Return dependency status without inventorying unrelated local packages."""
    health = dependency_health()
    installed = health.get("installed") if isinstance(health.get("installed"), dict) else {}
    expected = {
        name: {
            "required": required,
            "installed": installed.get(name),
            "matches": installed.get(name) == required,
        }
        for name, required in EXPECTED_RUNTIME_PINS.items()
    }
    unexpected = health.get("unexpected_distributions")
    unexpected_count = len(unexpected) if isinstance(unexpected, dict) else 0
    return {
        "status": str(health.get("status") or "unknown"),
        "expected": expected,
        "unexpected_distribution_count": unexpected_count,
    }


def yt_dlp_version() -> str:
    try:
        return importlib_metadata.version("yt-dlp")
    except importlib_metadata.PackageNotFoundError:
        return "not-installed"
    except Exception as exc:
        return f"unknown:{exc.__class__.__name__}"


def prepare_ytdlp_runtime() -> bool:
    """Disable external yt-dlp plugin discovery before importing extractors."""
    try:
        from yt_dlp.globals import all_plugins_loaded, plugin_dirs
        if bool(all_plugins_loaded.value) and list(plugin_dirs.value or []):
            logger.warning("yt-dlp plugins were initialized before isolation could be enforced.")
            return False
        plugin_dirs.value = []
        return True
    except Exception as exc:
        logger.warning(f"Could not enforce yt-dlp plugin isolation: {exc.__class__.__name__}")
        return False


def ytdlp_available() -> bool:
    if yt_dlp_version() == "not-installed":
        return False
    try:
        if not prepare_ytdlp_runtime():
            return False
        from yt_dlp import YoutubeDL  # noqa: F401
        from yt_dlp.globals import plugin_dirs
        return not bool(list(plugin_dirs.value or []))
    except Exception:
        return False


def executable_name(base: str) -> str:
    return base + (".exe" if os.name == "nt" else "")


def _ffmpeg_pair_from_dir(directory: Path) -> Optional[Tuple[Path, Path, Path]]:
    ffmpeg = directory / executable_name("ffmpeg")
    ffprobe = directory / executable_name("ffprobe")
    if ffmpeg.is_file() and ffprobe.is_file():
        return ffmpeg.resolve(), ffprobe.resolve(), directory.resolve()
    return None


def resolve_ffmpeg(location: str = "") -> Tuple[Optional[Path], Optional[Path], Optional[Path], str]:
    candidates: List[Tuple[Path, str]] = []
    if location:
        raw = Path(os.path.expandvars(os.path.expanduser(location)))
        if not raw.is_absolute():
            raw = APP_ROOT / raw
        if raw.is_file():
            candidates.append((raw.parent, "config-file"))
        else:
            candidates.append((raw, "config-directory"))
            candidates.append((raw / "bin", "config-bin-directory"))
    candidates.extend([
        (APP_ROOT / "ffmpeg" / "bin", "project-local"),
        (APP_ROOT / "tools" / "ffmpeg" / "bin", "project-local-tools"),
        (APP_ROOT / "ffmpeg", "project-local-flat"),
    ])
    seen: set[str] = set()
    for directory, source in candidates:
        key = str(directory).lower()
        if key in seen:
            continue
        seen.add(key)
        pair = _ffmpeg_pair_from_dir(directory)
        if pair:
            return pair[0], pair[1], pair[2], source
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        ffmpeg = Path(ffmpeg_path).resolve()
        ffprobe = Path(ffprobe_path).resolve()
        if ffmpeg.parent == ffprobe.parent:
            location_path = ffmpeg.parent
        else:
            location_path = None
        return ffmpeg, ffprobe, location_path, "PATH"
    return None, None, None, "not-found"


def command_first_line(command: Sequence[str], timeout: int = 8) -> str:
    try:
        completed = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return (completed.stdout or "").splitlines()[0][:300] if completed.stdout else ""
    except Exception as exc:
        return f"unavailable:{exc.__class__.__name__}"


def validate_public_url(value: str, allow_private_networks: bool = False, resolve_dns: bool = True) -> str:
    url = (value or "").strip()
    if not url:
        raise DownloaderError("A URL is required.")
    if len(url) > 8192 or any(character in url for character in ("\r", "\n", "\x00")):
        raise DownloaderError("The URL is too long or contains unsafe control characters.")
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise DownloaderError(f"Invalid URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise DownloaderError("Only http:// or https:// URLs are accepted.")
    if parsed.username or parsed.password:
        raise DownloaderError("URLs containing embedded usernames or passwords are not accepted.")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise DownloaderError(f"Invalid URL port: {exc}") from exc
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise DownloaderError("The URL does not contain a valid hostname.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        if not allow_private_networks:
            raise DownloaderError("Local/private network targets are blocked by default.")
    if allow_private_networks:
        return url

    def reject_address(address_text: str) -> None:
        try:
            address = ipaddress.ip_address(address_text.split("%", 1)[0])
        except ValueError:
            return
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise DownloaderError("Local/private/reserved network targets are blocked by default.")

    try:
        reject_address(host)
    except DownloaderError:
        raise
    if resolve_dns:
        try:
            records = socket.getaddrinfo(host, parsed_port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise DownloaderError(f"DNS lookup failed for {host}: {exc}") from exc
        addresses = {str(item[4][0]) for item in records if item and item[4]}
        if not addresses:
            raise DownloaderError(f"DNS lookup returned no addresses for {host}.")
        for address in addresses:
            reject_address(address)
    return url


def first_media_info(info: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(info, dict):
        return None
    entries = info.get("entries")
    if entries is not None:
        try:
            for entry in entries:
                found = first_media_info(entry)
                if found:
                    return found
        except TypeError:
            return None
        return None
    return info


def format_duration(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "unknown"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:d}:{secs:02d}"


def format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def safe_info_summary(info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": sanitize_untrusted_text(info.get("title") or info.get("id") or "untitled", 300),
        "extractor": sanitize_untrusted_text(info.get("extractor_key") or info.get("extractor") or "generic", 100),
        "media_id": sanitize_untrusted_text(info.get("id") or "", 200),
        "duration": format_duration(info.get("duration")),
        "duration_seconds": info.get("duration"),
        "uploader": sanitize_untrusted_text(info.get("uploader") or info.get("channel") or "", 200),
        "availability": sanitize_untrusted_text(info.get("availability") or "unknown", 100),
        "is_live": bool(info.get("is_live")),
        "has_drm": bool(info.get("has_drm")),
    }


def ensure_audio_candidate_info(info: Dict[str, Any], config: Dict[str, Any]) -> None:
    if bool(info.get("has_drm")):
        raise DownloaderError("The source reports DRM. This downloader does not bypass DRM.")
    availability = str(info.get("availability") or "").strip().lower()
    if availability and availability not in {"public", "unlisted"}:
        raise DownloaderError(
            f"The source reports restricted availability ({availability}). Login/paywall access is not supported."
        )
    live_status = str(info.get("live_status") or "").strip().lower()
    is_live = bool(info.get("is_live")) or live_status in {"is_live", "is_upcoming"}
    if is_live and not bool(config.get("allow_live_streams", False)):
        raise DownloaderError("Live or upcoming streams are blocked by default to prevent unbounded recording.")
    formats = info.get("formats") or []
    has_audio = str(info.get("acodec") or "none").lower() not in {"", "none"}
    if not has_audio and isinstance(formats, list):
        for item in formats:
            if isinstance(item, dict) and str(item.get("acodec") or "none").lower() not in {"", "none"}:
                has_audio = True
                break
    if not has_audio:
        raise DownloaderError("No audio stream was found for the submitted URL.")


def media_identity_digest(info: Dict[str, Any], original_url: str) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "generic").strip().lower()
    media_id = str(info.get("id") or "").strip()
    raw = f"{extractor}:{media_id}" if media_id else normalize_url_for_digest(original_url)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def duplicate_db_connect(path: Path = DUPLICATE_INDEX_PATH) -> sqlite3.Connection:
    ensure_dirs()
    connection = sqlite3.connect(str(path), timeout=DUPLICATE_DB_TIMEOUT_SECONDS)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS media_records (
            media_digest TEXT PRIMARY KEY,
            sha256 TEXT,
            path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            title TEXT,
            extractor TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_media_sha256 ON media_records(sha256)")
    connection.commit()
    return connection


def delete_indexed_media(media_digest: str) -> None:
    try:
        with duplicate_db_connect() as connection:
            connection.execute("DELETE FROM media_records WHERE media_digest = ?", (media_digest,))
            connection.commit()
    except Exception as exc:
        logger.debug(f"Duplicate index cleanup skipped: {exc}")


def find_indexed_media(media_digest: str) -> Optional[Path]:
    try:
        with duplicate_db_connect() as connection:
            row = connection.execute(
                "SELECT path, sha256, size_bytes FROM media_records WHERE media_digest = ?",
                (media_digest,),
            ).fetchone()
            if not row:
                return None
            path = Path(str(row[0]))
            expected_hash = str(row[1] or "").strip().lower()
            expected_size = int(row[2] or 0)
            valid = (
                path.is_file()
                and path.stat().st_size > 0
                and path.stat().st_size == expected_size
                and bool(re.fullmatch(r"[0-9a-f]{64}", expected_hash))
                and sha256_file(path).lower() == expected_hash
            )
            if valid:
                return path
            connection.execute("DELETE FROM media_records WHERE media_digest = ?", (media_digest,))
            connection.commit()
    except Exception as exc:
        logger.debug(f"Duplicate index lookup skipped: {exc}")
    return None


def reconcile_duplicate(
    media_digest: str,
    final_path: Path,
    title: str,
    extractor: str,
    enabled: bool,
) -> Tuple[Path, bool]:
    if not enabled:
        return final_path, False
    file_hash = sha256_file(final_path)
    size = final_path.stat().st_size
    now = utc_iso()
    try:
        with duplicate_db_connect() as connection:
            rows = connection.execute(
                "SELECT path FROM media_records WHERE sha256 = ? ORDER BY updated_utc DESC", (file_hash,)
            ).fetchall()
            existing: Optional[Path] = None
            for row in rows:
                candidate = Path(str(row[0]))
                if candidate.resolve() == final_path.resolve():
                    existing = final_path
                    break
                if (
                    candidate.is_file()
                    and candidate.stat().st_size == size
                    and sha256_file(candidate).lower() == file_hash
                ):
                    existing = candidate
                    break
            duplicate_skipped = False
            selected = final_path
            if existing is not None and existing.resolve() != final_path.resolve():
                final_path.unlink(missing_ok=True)
                selected = existing
                duplicate_skipped = True
            connection.execute(
                """
                INSERT INTO media_records(media_digest, sha256, path, size_bytes, title, extractor, created_utc, updated_utc)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_digest) DO UPDATE SET
                    sha256=excluded.sha256,
                    path=excluded.path,
                    size_bytes=excluded.size_bytes,
                    title=excluded.title,
                    extractor=excluded.extractor,
                    updated_utc=excluded.updated_utc
                """,
                (media_digest, file_hash, str(selected.resolve()), selected.stat().st_size, title[:300], extractor[:100], now, now),
            )
            connection.commit()
            return selected, duplicate_skipped
    except Exception as exc:
        logger.warning(f"Duplicate reconciliation could not complete: {exc}")
        return final_path, False


def load_host_tolerance() -> Dict[str, Any]:
    if not HOST_TOLERANCE_PATH.exists():
        return {"schema_version": 1, "hosts": {}}
    try:
        payload = json.loads(HOST_TOLERANCE_PATH.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(payload, dict) or int(payload.get("schema_version", 0)) != 1:
            return {"schema_version": 1, "hosts": {}}
        if not isinstance(payload.get("hosts"), dict):
            payload["hosts"] = {}
        return payload
    except Exception:
        return {"schema_version": 1, "hosts": {}}


def save_host_tolerance(payload: Dict[str, Any]) -> None:
    ensure_dirs()
    temp = HOST_TOLERANCE_PATH.with_name(
        f".{HOST_TOLERANCE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(HOST_TOLERANCE_PATH)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass


class HostToleranceFileLock:
    """Small bounded cross-process lock used only while updating host tolerance state."""

    def __init__(self, timeout_seconds: float = 10.0, stale_seconds: float = 60.0) -> None:
        self.timeout_seconds = max(0.5, timeout_seconds)
        self.stale_seconds = max(10.0, stale_seconds)
        self.acquired = False

    def __enter__(self):
        ensure_dirs()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fd = os.open(str(HOST_TOLERANCE_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"pid": os.getpid(), "created_utc": utc_iso()}, handle)
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - HOST_TOLERANCE_LOCK_PATH.stat().st_mtime
                    if age > self.stale_seconds:
                        HOST_TOLERANCE_LOCK_PATH.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for host-tolerance state lock")
                time.sleep(0.05 + random.uniform(0.0, 0.05))

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            try:
                HOST_TOLERANCE_LOCK_PATH.unlink(missing_ok=True)
            except Exception:
                pass
            self.acquired = False


def host_name(url: str) -> str:
    try:
        return (urlparse(url).hostname or "unknown-host").lower()
    except Exception:
        return "unknown-host"


def record_host_outcome(url: str, outcome: str, workers: int, error: str = "") -> None:
    host = host_name(url)
    try:
        with HOST_TOLERANCE_LOCK, HostToleranceFileLock():
            payload = load_host_tolerance()
            hosts = payload.setdefault("hosts", {})
            record = hosts.get(host)
            if not isinstance(record, dict):
                record = {"score": 0, "successes": 0, "failures": 0, "stalls": 0}
            score = int(record.get("score", 0) or 0)
            if outcome == "success":
                record["successes"] = int(record.get("successes", 0) or 0) + 1
                score = min(5, score + 1)
            elif outcome == "stall":
                record["stalls"] = int(record.get("stalls", 0) or 0) + 1
                score = max(-5, score - 3)
            else:
                record["failures"] = int(record.get("failures", 0) or 0) + 1
                score = max(-5, score - 2)
            record.update({
                "score": score,
                "last_outcome": outcome,
                "last_workers": int(workers),
                "last_error": redact_sensitive_text(error)[:240],
                "updated_utc": utc_iso(),
            })
            hosts[host] = record
            if len(hosts) > 100:
                ordered = sorted(
                    hosts.items(),
                    key=lambda item: str(item[1].get("updated_utc", "")),
                    reverse=True,
                )
                payload["hosts"] = dict(ordered[:100])
            save_host_tolerance(payload)
    except Exception as exc:
        logger.warning(f"Host-tolerance state update was skipped safely: {exc.__class__.__name__}")


def fragment_plan(config: Dict[str, Any], url: str) -> Dict[str, Any]:
    normal = coerce_int(config.get("adaptive_fragment_workers"), 3, 1, MAX_FRAGMENT_WORKERS)
    minimum = min(normal, coerce_int(config.get("adaptive_fragment_workers_min"), 1, 1, MAX_FRAGMENT_WORKERS))
    maximum = max(normal, coerce_int(config.get("adaptive_fragment_workers_max"), 5, 1, MAX_FRAGMENT_WORKERS))
    burst_successes = coerce_int(config.get("adaptive_fragment_burst_successes"), 3, 1, 20)
    try:
        active_jobs = max(1, int(os.environ.get("MP3DOWNLOADER_QUEUE_ACTIVE_JOBS", "1")))
    except ValueError:
        active_jobs = 1
    try:
        same_host_jobs = max(1, int(os.environ.get("MP3DOWNLOADER_QUEUE_SAME_HOST_JOBS", "1")))
    except ValueError:
        same_host_jobs = 1
    force_safe = os.environ.get("MP3DOWNLOADER_FORCE_SAFE_FRAGMENTS", "").strip() == "1"
    with HOST_TOLERANCE_LOCK:
        record = load_host_tolerance().get("hosts", {}).get(host_name(url), {})
    if not isinstance(record, dict):
        record = {}
    score = int(record.get("score", 0) or 0)
    successes = int(record.get("successes", 0) or 0)
    if force_safe:
        selected, reason = minimum, "watchdog_safe_restart"
    elif same_host_jobs >= 3:
        selected, reason = minimum, "three_same_host_jobs"
    elif same_host_jobs == 2:
        selected, reason = min(normal, 2), "two_same_host_jobs"
    elif score <= -2:
        selected, reason = minimum, "recent_host_pressure"
    elif score >= 3 and successes >= burst_successes and active_jobs == 1:
        selected, reason = maximum, "proven_single_job_tolerance"
    else:
        selected, reason = normal, "normal_tolerance"
    return {
        "host": host_name(url),
        "selected_workers": max(1, min(MAX_FRAGMENT_WORKERS, int(selected))),
        "normal_workers": normal,
        "min_workers": minimum,
        "max_workers": maximum,
        "active_jobs": active_jobs,
        "same_host_jobs": same_host_jobs,
        "host_score": score,
        "host_successes": successes,
        "reason": reason,
    }


class YtdlpLogger:
    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: " + redact_sensitive_text(str(message)))

    def info(self, message: str) -> None:
        logger.debug("yt-dlp: " + redact_sensitive_text(str(message)))

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp: " + redact_sensitive_text(str(message)))

    def error(self, message: str) -> None:
        logger.error("yt-dlp: " + redact_sensitive_text(str(message)))


def retry_sleep_function(config: Dict[str, Any]):
    base = float(config.get("retry_backoff_seconds", 1.5))
    jitter = float(config.get("retry_jitter_seconds", 0.75))
    cap = float(config.get("retry_after_cap_seconds", 120))

    def calculate(attempt: int) -> float:
        exponent = max(0, int(attempt))
        return min(cap, base * (2 ** exponent) + random.uniform(0.0, max(0.0, jitter)))

    return calculate


def common_ytdlp_options(config: Dict[str, Any], url: str) -> Dict[str, Any]:
    plan = fragment_plan(config, url)
    retry_sleep = retry_sleep_function(config)
    return {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "logger": YtdlpLogger(),
        "ignoreconfig": True,
        "noplaylist": True,
        "playlist_items": "1",
        "format": "bestaudio/best",
        "socket_timeout": int(config.get("timeout_seconds", 30)),
        "retries": int(config.get("retries", 5)),
        "fragment_retries": int(config.get("fragment_retries", 10)),
        "extractor_retries": int(config.get("retries", 5)),
        "file_access_retries": 3,
        "retry_sleep_functions": {
            "http": retry_sleep,
            "fragment": retry_sleep,
            "file_access": retry_sleep,
            "extractor": retry_sleep,
        },
        "concurrent_fragment_downloads": plan["selected_workers"],
        "max_filesize": int(float(config.get("max_size_mb", 2048)) * 1024 * 1024),
        "nocheckcertificate": not bool(config.get("verify_ssl", True)),
        "http_headers": {"User-Agent": str(config.get("user_agent") or DEFAULT_CONFIG["user_agent"])},
        "cachedir": str(STATE_DIR / "yt_dlp_cache"),
        "allow_unplayable_formats": False,
        "ignoreerrors": False,
        "geo_bypass": False,
        "windowsfilenames": True,
        "trim_file_name": 180,
        "progress_delta": 1.0,
        "usenetrc": False,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }


def extract_audio_info(url: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if not ytdlp_available():
        raise DownloaderError("The pinned yt-dlp dependency is unavailable. Install requirements.txt in the project environment.")
    from yt_dlp import YoutubeDL

    options = common_ytdlp_options(config, url)
    options.update({"skip_download": True, "simulate": True})
    mark_progress("preflight_extract")
    with YoutubeDL(options) as ydl:
        raw = ydl.extract_info(url, download=False)
    if isinstance(raw, dict) and (raw.get("entries") is not None or str(raw.get("_type") or "").lower() in {"playlist", "multi_video"}):
        raise DownloaderError("Playlist and multi-video URLs are not accepted. Submit one media item URL at a time.")
    info = first_media_info(raw)
    if not info:
        raise DownloaderError("No downloadable media information was returned for the submitted URL.")
    ensure_audio_candidate_info(info, config)
    webpage_url = str(info.get("webpage_url") or info.get("original_url") or "").strip()
    if webpage_url:
        validate_public_url(
            webpage_url,
            bool(config.get("allow_private_networks", False)),
            resolve_dns=True,
        )
    return info


def expected_mp3_path(info: Dict[str, Any], config: Dict[str, Any], out_dir: Path) -> Optional[Path]:
    if not ytdlp_available():
        return None
    from yt_dlp import YoutubeDL

    options = common_ytdlp_options(config, str(info.get("webpage_url") or "https://example.invalid/"))
    options.update({
        "paths": {"home": str(out_dir)},
        "outtmpl": {"default": str(config.get("output_filename_template") or DEFAULT_CONFIG["output_filename_template"])},
        "skip_download": True,
    })
    try:
        with YoutubeDL(options) as ydl:
            candidate = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
    except Exception:
        return None
    if candidate.is_symlink():
        raise DownloaderError(
            "The expected output filename is a symbolic link or reparse target. Remove it before downloading."
        )
    try:
        if candidate.exists() and int(candidate.stat().st_nlink) > 1:
            raise DownloaderError(
                "The expected output filename has multiple hard links. Move or rename it before downloading."
            )
    except DownloaderError:
        raise
    except OSError as exc:
        raise DownloaderError(f"The expected output path could not be inspected safely: {exc}") from exc
    if not path_is_within(candidate, out_dir):
        raise DownloaderError("The resolved output path would escape the selected output folder.")
    return candidate


def runtime_event_path(job_id: str) -> Optional[Path]:
    if not job_id:
        return None
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", job_id)[:60]
    return LOGS_DIR / "queue_jobs" / f"queue_{safe}_runtime.jsonl"


def emit_runtime_event(job_id: str, event: str, details: Optional[Dict[str, Any]] = None) -> None:
    path = runtime_event_path(job_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"utc": utc_iso(), "event": event, "details": redact_for_export(details or {})}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def path_tree_signature(paths: Iterable[Path]) -> Tuple[int, int, int]:
    total_size = 0
    newest_mtime = 0
    count = 0
    for root in paths:
        try:
            candidates = [root]
            if root.is_dir():
                candidates = [item for item in root.rglob("*") if item.is_file()]
            for item in candidates:
                try:
                    stat = item.stat()
                    total_size += int(stat.st_size)
                    newest_mtime = max(newest_mtime, int(stat.st_mtime_ns))
                    count += 1
                except Exception:
                    pass
        except Exception:
            pass
    return total_size, newest_mtime, count


class FileActivityMonitor:
    def __init__(self, job_id: str, watch_paths: Sequence[Path]) -> None:
        self.job_id = job_id
        self.watch_paths = list(watch_paths)
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.signature = path_tree_signature(self.watch_paths)

    def _run(self) -> None:
        while not self.stop_event.wait(3.0):
            signature = path_tree_signature(self.watch_paths)
            if signature != self.signature:
                self.signature = signature
                emit_runtime_event(self.job_id, "file_activity", {"signature": signature})

    def __enter__(self):
        if self.job_id:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop_event.set()


def gather_final_mp3(
    out_dir: Path,
    before: Dict[Path, Tuple[int, int]],
    expected: Optional[Path],
    observed: Sequence[Path],
    prepared: Optional[Path],
) -> Optional[Path]:
    candidates: List[Path] = []
    if expected is not None:
        try:
            resolved_expected = expected.resolve()
            if resolved_expected.is_file() and resolved_expected.stat().st_size > 0:
                previous = before.get(resolved_expected)
                current = (resolved_expected.stat().st_size, resolved_expected.stat().st_mtime_ns)
                if previous is None or previous != current:
                    return resolved_expected
        except Exception:
            pass
        candidates.append(expected)
    if prepared is not None:
        candidates.append(prepared.with_suffix(".mp3"))
    for path in observed:
        candidates.append(path if path.suffix.lower() == ".mp3" else path.with_suffix(".mp3"))
    expected_stem = expected.stem.casefold() if expected is not None else ""
    try:
        for path in out_dir.glob("*.mp3"):
            stat = path.stat()
            previous = before.get(path.resolve())
            changed = previous is None or previous != (stat.st_size, stat.st_mtime_ns)
            stem_matches = not expected_stem or path.stem.casefold() == expected_stem
            if changed and stem_matches:
                candidates.append(path)
    except Exception:
        pass
    unique: List[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            if resolved.is_file() and resolved.stat().st_size > 0:
                current = (resolved.stat().st_size, resolved.stat().st_mtime_ns)
                previous = before.get(resolved)
                if previous is not None and previous == current:
                    continue
                unique.append(resolved)
        except Exception:
            continue
    if not unique:
        return None
    unique.sort(key=lambda path: (path.stat().st_mtime_ns, path.stat().st_size), reverse=True)
    return unique[0]


def validate_mp3_output(path: Path, ffprobe: Path, max_size_mb: float) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise DownloaderError("Final MP3 validation failed: no non-empty output file was created.")
    max_bytes = int(max_size_mb * 1024 * 1024)
    if path.stat().st_size > max_bytes:
        raise DownloaderError(
            f"Final MP3 exceeds the configured size limit ({format_bytes(path.stat().st_size)} > {format_bytes(max_bytes)})."
        )
    try:
        completed = subprocess.run(
            [
                str(ffprobe), "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "default=nk=1:nw=1", str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except Exception as exc:
        raise DownloaderError(f"FFprobe validation could not run: {exc}") from exc
    codec = (completed.stdout or "").strip().lower()
    if completed.returncode != 0 or "mp3" not in codec:
        detail = (completed.stderr or completed.stdout or "unknown FFprobe error").strip()[:300]
        raise DownloaderError(f"Final output is not a validated MP3 audio stream: {detail}")


def apply_hidden_attribute(path: Path, enabled: bool) -> None:
    if not enabled or os.name != "nt":
        return
    try:
        subprocess.run(
            ["attrib", "+H", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=8,
        )
    except Exception:
        logger.warning("Could not apply the Windows Hidden attribute to the completed MP3.")


def prune_temp_root(current: Path, keep: int = 20, max_age_days: int = 14) -> None:
    root = TEMP_DIR / "yt_dlp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        directories = sorted(
            [path for path in root.iterdir() if path.is_dir()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    retained = 0
    for directory in directories:
        try:
            if directory.resolve() == current.resolve():
                continue
            modified = directory.stat().st_mtime
        except Exception:
            continue
        retained += 1
        if retained <= keep or modified >= cutoff:
            continue
        try:
            shutil.rmtree(directory)
        except Exception:
            pass


def quarantine_invalid_output(path: Path) -> Optional[Path]:
    """Remove the misleading .mp3 extension from a newly produced file that fails validation."""
    try:
        target = path.with_suffix(path.suffix + ".invalid")
        if target.exists():
            target = path.with_name(path.name + f".{local_filename_stamp()}.invalid")
        path.replace(target)
        return target
    except Exception:
        return None


def download_one(
    url: str,
    out_dir: Path,
    config: Dict[str, Any],
    quality_kbps: int,
    overwrite: bool,
    dry_run: bool,
    list_only: bool,
    queue_worker: bool,
    queue_job_id: str,
) -> Tuple[Optional[Path], bool]:
    url = validate_public_url(url, bool(config.get("allow_private_networks", False)), resolve_dns=True)
    append_run_history("preflight_start", url_descriptor(url))
    info = extract_audio_info(url, config)
    summary = safe_info_summary(info)
    print("")
    print("Audio-capable source found (authorization and publication status are not verified)")
    print(f"  Title: {summary['title']}")
    print(f"  Extractor: {summary['extractor']}")
    print(f"  Duration: {summary['duration']}")
    if summary.get("uploader"):
        print(f"  Uploader: {summary['uploader']}")
    print(f"  Reported availability: {summary['availability']}")
    print(f"  MP3 quality: {quality_kbps} kbps")
    append_run_history("preflight_complete", {**url_descriptor(url), **summary})
    if list_only or dry_run:
        print("")
        print("List-only complete. No file was downloaded." if list_only else "Dry-run complete. No file was downloaded.")
        return None, False

    ffmpeg, ffprobe, ffmpeg_location, ffmpeg_source = resolve_ffmpeg(str(config.get("ffmpeg_location") or ""))
    if not ffmpeg or not ffprobe:
        raise DownloaderError(
            "FFmpeg and FFprobe are required for MP3 conversion. Put both on PATH or in "
            f"{APP_ROOT / 'ffmpeg' / 'bin'}, then run the self-test again."
        )
    if not ytdlp_available():
        raise DownloaderError("The pinned yt-dlp dependency is unavailable. Install requirements.txt in the project environment.")
    from yt_dlp import YoutubeDL

    out_dir.mkdir(parents=True, exist_ok=True)
    media_digest = media_identity_digest(info, url)
    if bool(config.get("duplicate_detection_enabled", True)) and not overwrite:
        indexed = find_indexed_media(media_digest)
        if indexed is not None:
            try:
                validate_mp3_output(indexed, ffprobe, float(config.get("max_size_mb", 2048)))
            except DownloaderError as exc:
                delete_indexed_media(media_digest)
                logger.warning(f"Indexed duplicate failed MP3 validation and was unlinked from the index: {exc}")
            else:
                print(f"[SKIP] This media is already indexed and hash-verified: {indexed}")
                append_run_history("duplicate_media_skipped", {**url_descriptor(url), "path": safe_relative_name(indexed)})
                return indexed, True

    expected = expected_mp3_path(info, config, out_dir)
    if expected is not None and expected.is_file() and not overwrite:
        raise DownloaderError(
            "The expected output filename already exists but is not a verified index match for this media. "
            "Move or rename the existing file, choose another output folder, or use --overwrite intentionally."
        )
    url_hash = hashlib.sha256(normalize_url_for_digest(url).encode("utf-8", errors="ignore")).hexdigest()[:16]
    temp_key = re.sub(r"[^A-Za-z0-9_-]+", "_", queue_job_id)[:30] if queue_job_id else url_hash
    temp_dir = TEMP_DIR / "yt_dlp" / f"{temp_key}_{url_hash}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    prune_temp_root(temp_dir)
    before: Dict[Path, Tuple[int, int]] = {}
    for path in out_dir.glob("*.mp3"):
        try:
            stat = path.stat()
            before[path.resolve()] = (stat.st_size, stat.st_mtime_ns)
        except Exception:
            pass

    observed_paths: List[Path] = []
    last_progress_print = 0.0
    download_started = time.monotonic()
    max_size_bytes = int(float(config.get("max_size_mb", 2048)) * 1024 * 1024)

    def progress_hook(status: Dict[str, Any]) -> None:
        nonlocal last_progress_print
        state = str(status.get("status") or "")
        filename = status.get("filename")
        if filename:
            observed_paths.append(Path(str(filename)))
        if state == "downloading":
            downloaded = int(status.get("downloaded_bytes") or 0)
            total_raw = status.get("total_bytes") or status.get("total_bytes_estimate")
            try:
                total = int(total_raw) if total_raw else None
            except (TypeError, ValueError):
                total = None
            if downloaded > max_size_bytes or (total is not None and total > max_size_bytes):
                raise DownloaderError(
                    f"Download exceeded the configured size limit ({format_bytes(max(downloaded, total or 0))})."
                )
            now = time.monotonic()
            if now - last_progress_print >= 1.0:
                speed = status.get("speed")
                eta = status.get("eta")
                percent = (downloaded / total * 100.0) if total else None
                detail = f"{format_bytes(downloaded)}"
                if total:
                    detail += f" / {format_bytes(total)}"
                if percent is not None:
                    detail += f" ({percent:.1f}%)"
                if speed:
                    detail += f" at {format_bytes(speed)}/s"
                if eta is not None:
                    detail += f" ETA {int(eta)}s"
                if queue_worker:
                    print(f"[PROGRESS] {detail}", flush=True)
                else:
                    print("\r" + detail.ljust(100), end="", flush=True)
                emit_runtime_event(queue_job_id, "download_progress", {"downloaded_bytes": downloaded, "total_bytes": total})
                mark_progress("downloading")
                last_progress_print = now
        elif state == "finished":
            if not queue_worker:
                print("")
            print("[INFO] Source download finished; converting to MP3...", flush=True)
            emit_runtime_event(queue_job_id, "download_finished", {})
            mark_progress("postprocessing")

    def postprocessor_hook(status: Dict[str, Any]) -> None:
        state = str(status.get("status") or "")
        postprocessor = str(status.get("postprocessor") or "postprocessor")
        info_dict = status.get("info_dict") or {}
        for key in ("filepath", "filename", "_filename"):
            value = info_dict.get(key) if isinstance(info_dict, dict) else None
            if value:
                observed_paths.append(Path(str(value)))
        print(f"[POSTPROCESS] {postprocessor}: {state}", flush=True)
        emit_runtime_event(queue_job_id, "postprocessor", {"name": postprocessor, "status": state})
        mark_progress("postprocessing")

    base_options = common_ytdlp_options(config, url)
    base_options.update({
        "paths": {"home": str(out_dir), "temp": str(temp_dir)},
        "outtmpl": {"default": str(config.get("output_filename_template") or DEFAULT_CONFIG["output_filename_template"])},
        "skip_download": False,
        "simulate": False,
        "overwrites": bool(overwrite),
        "continuedl": True,
        "nopart": False,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(quality_kbps),
        }],
        "addmetadata": bool(config.get("write_metadata_tags", True)),
        "keepvideo": False,
    })
    if ffmpeg_location is not None:
        base_options["ffmpeg_location"] = str(ffmpeg_location)
    elif ffmpeg_source == "PATH":
        base_options["ffmpeg_location"] = str(ffmpeg.parent)

    plan = fragment_plan(config, url)
    append_run_history("download_start", {
        **url_descriptor(url),
        "quality_kbps": quality_kbps,
        "fragment_plan": plan,
        "ffmpeg_source": ffmpeg_source,
    })
    emit_runtime_event(queue_job_id, "worker_start", {**url_descriptor(url), "fragment_plan": plan})
    prepared: Optional[Path] = None
    last_error: Optional[BaseException] = None
    outer_attempts = max(1, int(config.get("outer_recovery_attempts", 1)) + 1)
    watch_paths: List[Path] = [temp_dir]
    if expected is not None:
        watch_paths.append(expected)

    with FileActivityMonitor(queue_job_id, watch_paths):
        for attempt in range(1, outer_attempts + 1):
            attempt_options = dict(base_options)
            if attempt > 1:
                attempt_options["concurrent_fragment_downloads"] = 1
                print(f"[RECOVERY] Retrying once in single-fragment mode ({attempt}/{outer_attempts})...", flush=True)
                emit_runtime_event(queue_job_id, "outer_retry", {"attempt": attempt})
            try:
                with YoutubeDL(attempt_options) as ydl:
                    raw_result = ydl.extract_info(url, download=True)
                    result = first_media_info(raw_result) or info
                    try:
                        prepared = Path(ydl.prepare_filename(result))
                    except Exception:
                        prepared = None
                last_error = None
                record_host_outcome(url, "success", int(attempt_options.get("concurrent_fragment_downloads", 1)))
                break
            except KeyboardInterrupt:
                raise
            except DownloaderError:
                raise
            except Exception as exc:
                last_error = exc
                message = redact_sensitive_text(str(exc))
                if attempt >= outer_attempts:
                    outcome = "stall" if any(token in message.lower() for token in ("timeout", "timed out", "stalled", "too slow")) else "failure"
                    record_host_outcome(url, outcome, int(attempt_options.get("concurrent_fragment_downloads", 1)), message)
                    break
                wait_seconds = min(
                    float(config.get("retry_after_cap_seconds", 120)),
                    float(config.get("retry_backoff_seconds", 1.5)) * attempt
                    + random.uniform(0.0, float(config.get("retry_jitter_seconds", 0.75))),
                )
                print(f"[WARN] Download backend interrupted: {message[:240]}", flush=True)
                print(f"[WARN] Rebuilding the backend after {wait_seconds:.1f}s...", flush=True)
                time.sleep(wait_seconds)

    if last_error is not None:
        if not bool(config.get("keep_partial_on_error", True)):
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise DownloaderError(f"Download failed after bounded recovery: {redact_sensitive_text(str(last_error))}")

    final_path = gather_final_mp3(out_dir, before, expected, observed_paths, prepared)
    if final_path is None:
        raise DownloaderError("yt-dlp completed without a discoverable MP3 output file.")
    final_path = validate_output_filesystem_state(final_path, out_dir)
    try:
        validate_mp3_output(final_path, ffprobe, float(config.get("max_size_mb", 2048)))
    except DownloaderError as exc:
        quarantined = quarantine_invalid_output(final_path)
        if quarantined is not None:
            raise DownloaderError(f"{exc} Invalid output was quarantined as {quarantined.name}.") from exc
        raise
    final_path = validate_output_filesystem_state(final_path, out_dir)
    selected_path, duplicate_skipped = reconcile_duplicate(
        media_digest=media_digest,
        final_path=final_path,
        title=str(info.get("title") or "")[:300],
        extractor=str(info.get("extractor_key") or info.get("extractor") or "generic")[:100],
        enabled=bool(config.get("duplicate_detection_enabled", True)),
    )
    apply_hidden_attribute(selected_path, bool(config.get("hide_completed_media", False)))
    shutil.rmtree(temp_dir, ignore_errors=True)
    elapsed = max(0.001, time.monotonic() - download_started)
    print("")
    if duplicate_skipped:
        print(f"[SKIP] Exact duplicate detected; existing MP3 retained: {selected_path}")
    else:
        print(f"[OK] MP3 saved: {selected_path}")
    print(f"[OK] Size: {format_bytes(selected_path.stat().st_size)} | elapsed: {elapsed:.1f}s")
    append_run_history("download_complete", {
        **url_descriptor(url),
        "path": safe_relative_name(selected_path),
        "size_bytes": selected_path.stat().st_size,
        "duplicate_skipped": duplicate_skipped,
        "quality_kbps": quality_kbps,
    })
    emit_runtime_event(queue_job_id, "worker_complete", {"path": safe_relative_name(selected_path)})
    return selected_path, duplicate_skipped


def sanitized_config_snapshot(config_path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        snapshot = validate_config(raw)
        if snapshot.get("ffmpeg_location"):
            snapshot["ffmpeg_location"] = "[CONFIGURED_PATH_REDACTED]"
        return redact_for_export(snapshot)
    except Exception as exc:
        return {"error": redact_sensitive_text(str(exc))}


def ffmpeg_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    ffmpeg, ffprobe, _, source = resolve_ffmpeg(str(config.get("ffmpeg_location") or ""))
    return {
        "available": bool(ffmpeg and ffprobe),
        "source": source,
        "ffmpeg_name": ffmpeg.name if ffmpeg else None,
        "ffprobe_name": ffprobe.name if ffprobe else None,
        "ffmpeg_version": command_first_line([str(ffmpeg), "-version"]) if ffmpeg else None,
        "ffprobe_version": command_first_line([str(ffprobe), "-version"]) if ffprobe else None,
    }


def create_support_export(config_path: Path, quiet: bool = False) -> Optional[Path]:
    """Create a compact status bundle without logs, history, media, URLs, or paths."""
    ensure_dirs()
    try:
        config = load_config(config_path)
    except Exception:
        config = dict(DEFAULT_CONFIG)
    summary = {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "release_channel": RELEASE_CHANNEL,
        "generated_utc": utc_iso(),
        "generated_local": local_label(),
        "rights_holder": RIGHTS_HOLDER,
        "copyright_notice": COPYRIGHT_NOTICE,
        "third_party_policy": THIRD_PARTY_POLICY,
        "support_export_schema": SUPPORT_EXPORT_SCHEMA,
        "terminal_status": TERMINAL_STATUS,
        "last_major_step": LAST_MAJOR_STEP,
        "last_progress_utc": utc_iso(LAST_PROGRESS_UTC),
        "platform": {
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "dependency_health": support_dependency_snapshot(),
        "yt_dlp_version": yt_dlp_version(),
        "ffmpeg": ffmpeg_snapshot(config),
        "config": sanitized_config_snapshot(config_path),
        "safety_boundaries": {
            "public_http_https_only": True,
            "initial_url_private_address_preflight": not bool(
                config.get("allow_private_networks", False)
            ),
            "downstream_request_containment": False,
            "egress_controls_required_for_untrusted_urls": True,
            "drm_bypass": False,
            "login_or_cookie_support": False,
            "paywall_bypass": False,
            "live_stream_recording_default": False,
            "playlists_default": False,
            "ffmpeg_bundled": False,
        },
    }
    export_path = EXPORTS_DIR / "mp3_downloader_support_export.zip"
    temp_path = export_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.comment = (
                f"MP3DOWNLOADER-SUPPORT|{APP_VERSION}|copyright={COPYRIGHT_NOTICE}|"
                f"third_party=preserve-notices-licenses"
            ).encode("utf-8")
            archive.writestr("support_summary.json", json.dumps(redact_for_export(summary), indent=2, ensure_ascii=False) + "\n")
            if config_path.exists():
                archive.writestr("config_redacted.json", json.dumps(sanitized_config_snapshot(config_path), indent=2, ensure_ascii=False) + "\n")
        temp_path.replace(export_path)
        hash_value = sha256_file(export_path)
        sidecar = export_path.with_suffix(export_path.suffix + ".sha256.txt")
        sidecar.write_text(
            "# MP3 Downloader support-export checksum\n"
            f"# {COPYRIGHT_NOTICE}\n"
            "# Third-party components retain their own copyright notices and licenses.\n"
            f"{hash_value}  {export_path.name}\n",
            encoding="utf-8",
        )
        if not quiet:
            print(f"[OK] Support export created: {export_path}")
            print(f"[OK] SHA256: {hash_value}")
        return export_path
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        if not quiet:
            print(f"[ERROR] Support export failed: {redact_sensitive_text(str(exc))}")
        return None


def ffmpeg_conversion_self_test(ffmpeg: Path, ffprobe: Path) -> Tuple[bool, str]:
    test_dir = TEMP_DIR / "self_test"
    test_dir.mkdir(parents=True, exist_ok=True)
    output = test_dir / "ffmpeg_self_test.mp3"
    try:
        completed = subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=1000:duration=0.20",
                "-codec:a", "libmp3lame", "-b:a", "64k", str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return False, (completed.stderr or completed.stdout or "FFmpeg returned a failure")[:500]
        validate_mp3_output(output, ffprobe, 10.0)
        return True, "FFmpeg MP3 encode and FFprobe validation passed"
    except Exception as exc:
        return False, redact_sensitive_text(str(exc))
    finally:
        try:
            output.unlink(missing_ok=True)
            test_dir.rmdir()
        except Exception:
            pass


def self_test(config_path: Path, verbose: bool = False) -> int:
    setup_logging(verbose=verbose)
    ensure_dirs()
    failures: List[str] = []
    print(f"{APP_NAME} self-test")
    print(f"Version: {APP_VERSION}")
    print(f"Project folder: {APP_ROOT}")
    if sync_config_file(config_path) != 0:
        failures.append("config synchronization")
        config = dict(DEFAULT_CONFIG)
    else:
        try:
            config = load_config(config_path)
            print("[OK] Config validation passed.")
        except Exception as exc:
            config = dict(DEFAULT_CONFIG)
            failures.append(f"config validation: {exc}")
    health = dependency_health()
    if health.get("status") == "verified":
        print("[OK] Exact pinned dependency lock verified.")
    else:
        failures.append("dependency lock mismatch")
        for item in health.get("mismatches", []):
            print(f"[FAIL] {item['package']}: required {item['required']}, installed {item['installed']}")
    if ytdlp_available():
        print(f"[OK] yt-dlp available and external plugin discovery disabled: {yt_dlp_version()}")
    else:
        failures.append("yt-dlp unavailable or plugin isolation failed")
    ffmpeg, ffprobe, _, source = resolve_ffmpeg(str(config.get("ffmpeg_location") or ""))
    if ffmpeg and ffprobe:
        print(f"[OK] FFmpeg/FFprobe located via {source}.")
        ok, detail = ffmpeg_conversion_self_test(ffmpeg, ffprobe)
        if ok:
            print(f"[OK] {detail}")
        else:
            failures.append("FFmpeg MP3 conversion")
            print(f"[FAIL] FFmpeg self-test: {detail}")
    else:
        failures.append("FFmpeg/FFprobe not found")
        print("[FAIL] FFmpeg and FFprobe were not found.")
    if APP_VERSION == "1.0.0" and RIGHTS_HOLDER and COPYRIGHT_NOTICE:
        print("[OK] Public release identity and rights notice verified.")
    else:
        failures.append("public release identity")
    try:
        test_db = TEMP_DIR / "self_test_index.sqlite3"
        with duplicate_db_connect(test_db) as connection:
            connection.execute("SELECT 1").fetchone()
        test_db.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(test_db) + suffix).unlink(missing_ok=True)
        print("[OK] SQLite duplicate-index capability verified.")
    except Exception as exc:
        failures.append(f"SQLite: {exc}")
    try:
        validate_public_url("https://example.com/media", allow_private_networks=False, resolve_dns=False)
        try:
            validate_public_url("http://127.0.0.1/test", allow_private_networks=False, resolve_dns=False)
            failures.append("private network URL guard")
        except DownloaderError:
            pass
        print("[OK] Initial URL scheme and address preflight verified.")
    except Exception as exc:
        failures.append(f"URL guard: {exc}")
    if failures:
        print("")
        print("[FAIL] Self-test found problems:")
        for failure in failures:
            print(f"  - {failure}")
        append_run_history("self_test_failed", {"failures": failures})
        return 1
    print("")
    print("[OK] Self-test passed.")
    append_run_history("self_test_passed", {})
    return 0


def queue_console_tail(path: Path, max_chars: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-8000:]
        lines = [part.strip() for part in re.split(r"[\r\n]+", text) if part.strip()]
        return redact_sensitive_text(lines[-1])[-max_chars:] if lines else ""
    except Exception:
        return ""


def queue_job_activity_signature(item: Dict[str, Any]) -> Tuple[int, int, int]:
    paths: List[Path] = []
    for key in ("console_path", "runtime_path"):
        value = item.get(key)
        if isinstance(value, Path):
            paths.append(value)
    temp_path = item.get("temp_watch_path")
    if isinstance(temp_path, Path):
        paths.append(temp_path)
    return path_tree_signature(paths)


def terminate_process_tree(process: Any, force: bool = False) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        command = ["taskkill", "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
            return
        except Exception:
            pass
    try:
        process.kill() if force else process.terminate()
    except Exception:
        pass


def queue_worker_command(args: argparse.Namespace, config_path: Path, job_id: str) -> List[str]:
    command = [
        sys.executable, "-u", str(Path(__file__).resolve()),
        "--easy", "--queue-worker", "--queue-job-id", job_id,
        "--config", str(config_path), "--out", str(args.out),
    ]
    if args.quality_kbps is not None:
        command.extend(["--quality-kbps", str(args.quality_kbps)])
    if args.max_size_mb is not None:
        command.extend(["--max-size-mb", str(args.max_size_mb)])
    if args.overwrite:
        command.append("--overwrite")
    if args.dry_run:
        command.append("--dry-run")
    if args.verbose:
        command.append("--verbose")
    return command


def start_queue_process(item: Dict[str, Any], append_console: bool = False, force_safe: bool = False) -> None:
    mode = "a" if append_console else "w"
    handle = item["console_path"].open(mode, encoding="utf-8", errors="replace")
    if append_console:
        handle.write("\n[WATCHDOG] Restarting once in safe single-fragment mode; resumable partial state is retained.\n")
        handle.flush()
    env = dict(item["env"])
    if force_safe:
        env["MP3DOWNLOADER_FORCE_SAFE_FRAGMENTS"] = "1"
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            item["command"],
            cwd=str(APP_ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
    except Exception:
        handle.close()
        raise
    item["process"] = process
    item["console_handle"] = handle
    item["last_activity_monotonic"] = time.monotonic()
    item["last_activity_utc"] = utc_iso()
    item["activity_signature"] = queue_job_activity_signature(item)


def refresh_queue_activity(item: Dict[str, Any]) -> float:
    now = time.monotonic()
    signature = queue_job_activity_signature(item)
    if signature != item.get("activity_signature"):
        item["activity_signature"] = signature
        item["last_activity_monotonic"] = now
        item["last_activity_utc"] = utc_iso()
    return max(0.0, now - float(item.get("last_activity_monotonic", now)))


def restart_stalled_job(item: Dict[str, Any]) -> None:
    terminate_process_tree(item.get("process"), force=False)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and item.get("process") is not None and item["process"].poll() is None:
        time.sleep(0.2)
    terminate_process_tree(item.get("process"), force=True)
    try:
        item["console_handle"].flush()
        item["console_handle"].close()
    except Exception:
        pass
    item["restart_count"] = int(item.get("restart_count", 0)) + 1
    item["status"] = "running"
    item["exit_code"] = None
    item["finished_utc"] = None
    start_queue_process(item, append_console=True, force_safe=True)


def write_queue_state(jobs: Sequence[Dict[str, Any]], status: str, idle_timeout: int, restart_on_stall: bool) -> None:
    ensure_dirs()
    now = time.monotonic()
    payload = {
        "schema_version": 1,
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "updated_utc": utc_iso(),
        "updated_local": local_label(),
        "session_status": status,
        "max_links_per_session": MAX_PARALLEL_LINKS,
        "watchdog_policy": {
            "idle_timeout_seconds": idle_timeout,
            "restart_once_in_safe_mode": restart_on_stall,
        },
        "jobs": [
            {
                "job_id": item.get("job_id"),
                "slot": item.get("slot"),
                "source_host": item.get("source_host"),
                "url_sha256_prefix": item.get("url_sha256_prefix"),
                "status": item.get("status"),
                "pid": getattr(item.get("process"), "pid", None),
                "exit_code": item.get("exit_code"),
                "started_utc": item.get("started_utc"),
                "finished_utc": item.get("finished_utc"),
                "last_activity_utc": item.get("last_activity_utc"),
                "idle_seconds": round(max(0.0, now - float(item.get("last_activity_monotonic", now))), 1),
                "restart_count": int(item.get("restart_count", 0)),
                "last_console_line": queue_console_tail(item["console_path"]),
            }
            for item in jobs
        ],
    }
    try:
        temp = QUEUE_STATE_PATH.with_suffix(".json.tmp")
        temp.write_text(json.dumps(redact_for_export(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(QUEUE_STATE_PATH)
    except Exception:
        pass


def monitor_jobs_once(
    jobs: List[Dict[str, Any]],
    idle_timeout: int,
    restart_on_stall: bool,
) -> None:
    for item in jobs:
        process = item.get("process")
        if process is None or item.get("status") not in {"running", "restarting"}:
            continue
        exit_code = process.poll()
        if exit_code is not None:
            item["exit_code"] = int(exit_code)
            item["finished_utc"] = utc_iso()
            item["status"] = "completed" if exit_code == 0 else "failed"
            try:
                item["console_handle"].flush()
                item["console_handle"].close()
            except Exception:
                pass
            continue
        idle_seconds = refresh_queue_activity(item)
        if idle_seconds < idle_timeout:
            continue
        if restart_on_stall and int(item.get("restart_count", 0)) < 1:
            print(f"[WATCHDOG] Link {item['slot']} inactive for {int(idle_seconds)}s; restarting once safely.")
            item["status"] = "restarting"
            restart_stalled_job(item)
        else:
            print(f"[WATCHDOG] Link {item['slot']} inactive again; stopping it cleanly.")
            terminate_process_tree(process, force=False)
            time.sleep(1.0)
            terminate_process_tree(process, force=True)
            item["status"] = "failed_stall"
            item["exit_code"] = 124
            item["finished_utc"] = utc_iso()


def prompt_while_monitoring(
    prompt: str,
    jobs: List[Dict[str, Any]],
    idle_timeout: int,
    restart_on_stall: bool,
) -> str:
    result: Dict[str, str] = {}
    done = threading.Event()

    def reader() -> None:
        try:
            result["value"] = input(prompt)
        except EOFError:
            result["value"] = ""
        finally:
            done.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    last_status = 0.0
    while not done.wait(1.0):
        monitor_jobs_once(jobs, idle_timeout, restart_on_stall)
        now = time.monotonic()
        if jobs and now - last_status >= 15.0:
            running = [item for item in jobs if item.get("status") in {"running", "restarting"}]
            if running:
                labels = ", ".join(
                    f"{item['slot']}:{item['source_host']} idle={int(refresh_queue_activity(item))}s"
                    for item in running
                )
                print(f"\n[RUNNING] {labels}")
                print(prompt, end="", flush=True)
            write_queue_state(jobs, "collecting_links", idle_timeout, restart_on_stall)
            last_status = now
    return result.get("value", "")


def run_interactive_link_queue(config_path: Path, args: argparse.Namespace) -> int:
    ensure_dirs()
    if sync_config_file(config_path) != 0:
        return 1
    config = load_config(config_path)
    setup_logging(verbose=bool(args.verbose))
    set_terminal_status("link_queue_running")
    guard = InstanceGuard(enabled=bool(config.get("single_instance_guard", True)))
    if not guard.acquire():
        return 2
    queue_dir = LOGS_DIR / "queue_jobs"
    queue_dir.mkdir(parents=True, exist_ok=True)
    idle_timeout = int(config.get("queue_worker_idle_timeout_seconds", 600))
    restart_on_stall = bool(config.get("queue_worker_restart_on_stall", True))
    jobs: List[Dict[str, Any]] = []
    url_hashes: set[str] = set()
    print("")
    print("MP3 Easy Link Queue")
    print("Paste up to three authorized HTTP(S) media URLs. Each starts immediately.")
    print("Blank input stops adding links. Playlists, live streams, DRM, login walls, and paywalls are not supported.")
    try:
        slot = 1
        while slot <= MAX_PARALLEL_LINKS:
            prompt = f"Link {slot}: " if slot == 1 else f"Link {slot} (blank to finish): "
            raw = prompt_while_monitoring(prompt, jobs, idle_timeout, restart_on_stall).strip()
            if not raw:
                break
            try:
                url = validate_public_url(raw, bool(config.get("allow_private_networks", False)), resolve_dns=True)
            except DownloaderError as exc:
                print(f"[ERROR] {exc}")
                continue
            descriptor = url_descriptor(url)
            if descriptor["url_sha256_prefix"] in url_hashes:
                print("[WARN] That URL is already in this queue; duplicate submission skipped.")
                continue
            url_hashes.add(descriptor["url_sha256_prefix"])
            job_id = f"link{slot}_{RUN_ID}"
            command = queue_worker_command(args, config_path, job_id)
            same_host_jobs = 1 + sum(1 for item in jobs if item.get("source_host") == descriptor["host"])
            env = dict(os.environ)
            env["MP3DOWNLOADER_QUEUE_ACTIVE_JOBS"] = str(len(jobs) + 1)
            env["MP3DOWNLOADER_QUEUE_SAME_HOST_JOBS"] = str(same_host_jobs)
            env["MP3DOWNLOADER_QUEUE_URL"] = url
            item: Dict[str, Any] = {
                "job_id": job_id,
                "slot": slot,
                "url": url,
                "source_host": descriptor["host"],
                "url_sha256_prefix": descriptor["url_sha256_prefix"],
                "command": command,
                "env": env,
                "console_path": queue_dir / f"queue_{job_id}_console.log",
                "runtime_path": runtime_event_path(job_id),
                "temp_watch_path": TEMP_DIR / "yt_dlp" / f"{job_id}_{descriptor['url_sha256_prefix']}",
                "status": "running",
                "started_utc": utc_iso(),
                "finished_utc": None,
                "exit_code": None,
                "restart_count": 0,
            }
            start_queue_process(item)
            jobs.append(item)
            print(f"[STARTED] Link {slot}: {descriptor['host']} (PID {item['process'].pid})")
            write_queue_state(jobs, "collecting_links", idle_timeout, restart_on_stall)
            slot += 1
        if not jobs:
            print("No links were queued.")
            set_terminal_status("completed")
            return 0
        print("")
        print("All submitted links are running. Ctrl+C stops the queue and preserves resumable partial state.")
        last_status = 0.0
        while any(item.get("status") in {"running", "restarting"} for item in jobs):
            monitor_jobs_once(jobs, idle_timeout, restart_on_stall)
            now = time.monotonic()
            if now - last_status >= 15.0:
                running = [item for item in jobs if item.get("status") in {"running", "restarting"}]
                if running:
                    labels = ", ".join(
                        f"{item['slot']}:{item['source_host']} idle={int(refresh_queue_activity(item))}s"
                        for item in running
                    )
                    print(f"[RUNNING] {labels}")
                write_queue_state(jobs, "running", idle_timeout, restart_on_stall)
                last_status = now
            time.sleep(1.0)
        successes = sum(1 for item in jobs if item.get("status") == "completed")
        failures = len(jobs) - successes
        set_terminal_status("completed" if failures == 0 else ("completed_with_warnings" if successes else "failed_downloads"))
        write_queue_state(jobs, TERMINAL_STATUS, idle_timeout, restart_on_stall)
        print("")
        print(f"Queue complete: {successes} succeeded, {failures} failed.")
        for item in jobs:
            print(f"  Link {item['slot']} {item['source_host']}: {item['status']} | {queue_console_tail(item['console_path'])}")
        append_run_history("link_queue_complete", {
            "submitted": len(jobs), "successful": successes, "failed": failures,
            "watchdog_restarts": sum(int(item.get("restart_count", 0)) for item in jobs),
        })
        return 0 if failures == 0 else (3 if successes else 1)
    except KeyboardInterrupt:
        set_terminal_status("cancelled")
        print("\nCancelling active link downloads...")
        for item in jobs:
            terminate_process_tree(item.get("process"), force=False)
        time.sleep(1.0)
        for item in jobs:
            terminate_process_tree(item.get("process"), force=True)
            item["status"] = "cancelled"
            item["finished_utc"] = utc_iso()
        write_queue_state(jobs, "cancelled", idle_timeout, restart_on_stall)
        return 1
    finally:
        for item in jobs:
            try:
                handle = item.get("console_handle")
                if handle is not None:
                    handle.close()
            except Exception:
                pass
        guard.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download the best available audio stream from one authorized HTTP(S) URL and convert it to MP3. "
            "No DRM, login, cookie, paywall, or anti-bot bypass behavior is included. "
            "The initial URL guard is not downstream network containment."
        )
    )
    parser.add_argument("--url", help="Authorized webpage or direct media URL.")
    parser.add_argument("--out", default=str(DOWNLOADS_DIR), help="Output folder for MP3 files.")
    parser.add_argument("--easy", action="store_true", help="Download one URL and exit.")
    parser.add_argument("--link-queue", action="store_true", help="Start up to three independent authorized URLs.")
    parser.add_argument("--list-only", action="store_true", help="Inspect the URL without downloading.")
    parser.add_argument("--dry-run", action="store_true", help="Perform source preflight without downloading.")
    parser.add_argument("--quality-kbps", type=int, default=None, help="MP3 bitrate from 64 through 320 kbps.")
    parser.add_argument("--max-size-mb", type=float, default=None, help="Maximum source/final file size in MB.")
    parser.add_argument("--overwrite", action="store_true", help="Allow yt-dlp to overwrite the expected output name.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logs.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.json")
    parser.add_argument("--self-test", action="store_true", help="Run local dependency, FFmpeg, release-identity, and safety tests.")
    parser.add_argument("--export-support", action="store_true", help="Create a compact redacted support ZIP.")
    parser.add_argument("--sync-config", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dependency-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ffmpeg-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--queue-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--queue-job-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = APP_ROOT / config_path
    if args.version:
        print(f"{APP_NAME} {APP_VERSION} ({RELEASE_CHANNEL})")
        return 0
    if args.sync_config:
        return sync_config_file(config_path)
    if args.dependency_check:
        health = dependency_health()
        if health.get("status") == "verified":
            print("[OK] Exact pinned dependency lock verified.")
            return 0
        print("[WARN] Exact pinned dependency repair is required.")
        for item in health.get("mismatches", []):
            print(f"  - {item['package']}: required {item['required']}, installed {item['installed']}")
        return 10
    if args.ffmpeg_check:
        try:
            config = load_config(config_path)
        except Exception:
            config = dict(DEFAULT_CONFIG)
        ffmpeg, ffprobe, _, source = resolve_ffmpeg(str(config.get("ffmpeg_location") or ""))
        if ffmpeg and ffprobe:
            print(f"[OK] FFmpeg and FFprobe detected via {source}.")
            return 0
        print("[WARN] FFmpeg and FFprobe were not found through config, project-local folders, or PATH.")
        return 11
    if args.link_queue:
        if args.url or args.queue_worker:
            parser.error("--link-queue cannot be combined with --url or --queue-worker")
        return run_interactive_link_queue(config_path, args)

    ensure_dirs()
    if args.queue_worker:
        if not config_path.exists():
            print(f"[ERROR] Queue worker config was not found: {config_path}")
            return 1
    elif sync_config_file(config_path) != 0:
        return 1
    if args.export_support:
        return 0 if create_support_export(config_path, quiet=False) else 1
    if args.self_test:
        return self_test(config_path, verbose=args.verbose)

    try:
        config = load_config(config_path)
    except Exception as exc:
        setup_logging(verbose=args.verbose)
        print(f"[ERROR] Could not load config: {redact_sensitive_text(str(exc))}")
        return 1
    if args.max_size_mb is not None:
        config["max_size_mb"] = coerce_float(args.max_size_mb, float(config["max_size_mb"]), 10.0, 10240.0)
    quality = coerce_int(
        args.quality_kbps if args.quality_kbps is not None else config.get("mp3_quality_kbps", 192),
        192, 64, 320,
    )
    overwrite = bool(args.overwrite or config.get("overwrite", False))
    dry_run = bool(args.dry_run or config.get("dry_run", False))
    setup_logging(verbose=args.verbose)
    set_terminal_status("running")
    guard = InstanceGuard(enabled=bool(config.get("single_instance_guard", True)) and not bool(args.queue_worker))
    if not guard.acquire():
        set_terminal_status("blocked_by_instance_guard")
        return 2
    append_run_history("run_start", {
        "url_present": bool(args.url),
        "queue_worker": bool(args.queue_worker),
        "dry_run": dry_run,
        "list_only": bool(args.list_only),
    })
    try:
        queue_env_url = os.environ.pop("MP3DOWNLOADER_QUEUE_URL", "") if args.queue_worker else ""
        url = (args.url or queue_env_url or "").strip()
        if not url:
            if args.queue_worker:
                raise DownloaderError("Queue worker URL was not supplied by the parent process.")
            print("Download only media you are authorized to save. DRM, login walls, paywalls, playlists, and live streams are not supported.")
            url = input("Authorized media URL: ").strip()
        out_dir = Path(args.out)
        if not out_dir.is_absolute():
            out_dir = APP_ROOT / out_dir
        path, duplicate = download_one(
            url=url,
            out_dir=out_dir,
            config=config,
            quality_kbps=quality,
            overwrite=overwrite,
            dry_run=dry_run,
            list_only=bool(args.list_only),
            queue_worker=bool(args.queue_worker),
            queue_job_id=str(args.queue_job_id or ""),
        )
        set_terminal_status("completed")
        append_run_history("run_complete", {
            "output": safe_relative_name(path) if path else None,
            "duplicate_skipped": duplicate,
            "mode": "list_or_dry" if not path else "download",
        })
        return 0
    except DownloaderError as exc:
        set_terminal_status("failed")
        logger.error(redact_sensitive_text(str(exc)))
        append_run_history("run_failed", {"category": "expected", "error": str(exc)})
        emit_runtime_event(str(args.queue_job_id or ""), "worker_failed", {"error": str(exc)})
        return 1
    except KeyboardInterrupt:
        set_terminal_status("cancelled")
        logger.warning("Cancelled by user. Resumable partial state was preserved where available.")
        append_run_history("run_cancelled", {})
        return 1
    except Exception as exc:
        set_terminal_status("failed_unexpected")
        logger.exception(f"Unexpected error: {redact_sensitive_text(str(exc))}")
        append_run_history("run_failed", {"category": "unexpected", "error": str(exc)})
        emit_runtime_event(str(args.queue_job_id or ""), "worker_failed", {"error": str(exc)})
        return 1
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
