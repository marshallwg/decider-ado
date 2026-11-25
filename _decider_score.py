#!/usr/bin/env python3
"""Python scoring engine bundled with the Stata `decider` package.

Supports structured probabilistic decisions (`choice`, `score`, `noul`) over
tabular text exported from Stata. Uses only the Python standard library for the
hosted OpenRouter backend and offline mock backend; imports MLX / NeoJev only
when `backend == "neojev"` is explicitly requested on Apple Silicon.

Design invariants:
  - Explicit cloud opt-in: OpenRouter refuses to run unless `allow_cloud` is true.
  - Credentials never touch disk: `OPENROUTER_API_KEY` is read from the environment.
  - Row-ID checkpointing: each completed row is flushed immediately under a lock,
    enabling safe parallel execution on OpenRouter and crash-safe `--resume`.
  - No silent fallback: failures stop the run; timeouts are never auto-retried.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_NEOJEV_MODEL = "dowands/NeoJev-Qwen3.8-27B-L56-MLX"
DEFAULT_OPENROUTER_MODEL = "typesafe/jev-1.14"
DECIDER_HOME = Path.home() / ".decider"
MANAGED_VENV = DECIDER_HOME / "venv"
MANAGED_VENDOR_DIR = DECIDER_HOME / "vendor" / "neojev"
NEOJEV_HELPER_FILES = ("decider.py", "vision_common.py", "vision_mlx.py")
NEOJEV_DEPENDENCIES = (
    "huggingface-hub>=1.0,<2",
    "mlx==0.32.2",
    "mlx-lm==0.31.3",
    "numpy>=2,<3",
    "pillow>=12,<14",
)

VALID_TYPES = ("choice", "score", "noul")
VALID_BACKENDS = ("neojev", "openrouter", "mock")


# ---------------------------------------------------------------------------
# Provenance & filesystem helpers
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_float(value: Any, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    return float(value)


def _parse_int(value: Any, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    return int(float(value))


# ---------------------------------------------------------------------------
# Config & input preparation (reads Stata handoff files -> frozen JSON)
# ---------------------------------------------------------------------------

def load_or_freeze_config(run_dir: Path) -> dict[str, Any]:
    """Freeze raw Stata handoff files into canonical `config.json` once."""
    config_json = run_dir / "config.json"
    if config_json.exists():
        return json.loads(config_json.read_text(encoding="utf-8"))

    raw_cfg_path = run_dir / "config_raw.txt"
    if not raw_cfg_path.exists():
        raise FileNotFoundError(f"Missing {raw_cfg_path} and {config_json}.")

    raw: dict[str, str] = {}
    for line in raw_cfg_path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        raw[k.strip()] = v.strip()

    qtype = raw.get("type", "").lower()
    if qtype not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}, got {qtype!r}.")

    backend = raw.get("backend", "neojev").lower()
    if backend not in VALID_BACKENDS:
        raise ValueError(f"backend must be one of {VALID_BACKENDS}, got {backend!r}.")

    # Resolve prompt text: either inline prompt.txt written by Stata or external promptfile.
    prompt_file = raw.get("prompt_file", "")
    if prompt_file:
        pf = Path(prompt_file)
        if not pf.is_file():
            raise FileNotFoundError(f"Prompt file not found: {pf}")
        prompt_text = pf.read_text(encoding="utf-8")
    else:
        inline_path = run_dir / "prompt.txt"
        if not inline_path.is_file():
            raise ValueError("Either prompt() or promptfile() must be provided.")
        prompt_text = inline_path.read_text(encoding="utf-8")

    if not prompt_text.strip():
        raise ValueError("Prompt text is empty.")

    # Freeze an immutable copy of the prompt inside the run folder.
    (run_dir / "frozen_prompt.txt").write_text(prompt_text, encoding="utf-8")
    prompt_hash = sha256_text(prompt_text)

    # Read options list (one per line) for choice/score.
    options: list[str] = []
    options_path = run_dir / "options.txt"
    if options_path.is_file():
        options = [ln for ln in options_path.read_text(encoding="utf-8").splitlines() if ln != ""]

    if qtype in ("choice", "score"):
        if len(options) < 2:
            raise ValueError(f"type({qtype}) requires at least two options.")
        if len(options) > 16:
            raise ValueError(f"At most 16 options are supported; got {len(options)}.")
        if len(set(options)) != len(options):
            raise ValueError("Duplicate option labels are not allowed.")
    elif options:
        raise ValueError("options() is not used with type(noul).")

    varlist = [v for v in raw.get("varlist", "").split() if v]
    if not varlist:
        raise ValueError("At least one input variable is required.")

    default_model = (
        DEFAULT_NEOJEV_MODEL if backend == "neojev"
        else DEFAULT_OPENROUTER_MODEL if backend == "openrouter"
        else "mock/deterministic-v1"
    )
    model = raw.get("model", "") or default_model
    workers = _parse_int(raw.get("workers"), 1)
    if workers < 1:
        raise ValueError("workers() must be a positive integer.")
    if backend == "neojev" and workers > 1:
        raise ValueError(
            "workers() > 1 is not supported for backend(neojev): local MLX is "
            "GPU-bound and single-threaded. Use workers(1) for local runs."
        )

    config: dict[str, Any] = {
        "created_at_utc": utc_now_iso(),
        "type": qtype,
        "prefix": raw.get("prefix", ""),
        "varlist": varlist,
        "backend": backend,
        "allow_cloud": _parse_bool(raw.get("allow_cloud"), False),
        "model": model,
        "prompt_file": prompt_file,
        "prompt_text": prompt_text,
        "prompt_sha256": prompt_hash,
        "options": options,
        "reverse_check": _parse_bool(raw.get("reverse_check"), False),
        "min_prob": _parse_float(raw.get("min_prob"), 0.80),
        "min_margin": _parse_float(raw.get("min_margin"), 0.30),
        "threshold": _parse_float(raw.get("threshold"), 0.80),
        "token_budget": _parse_int(raw.get("token_budget"), 4096),
        "timeout_seconds": _parse_int(raw.get("timeout_seconds"), 60),
        "workers": workers,
        "vendor_dir": raw.get("vendor_dir", ""),
    }

    if qtype == "noul" and config["reverse_check"]:
        raise ValueError("reversecheck is only valid for type(choice) or type(score).")

    config_json.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return config


def load_or_freeze_inputs(run_dir: Path, varlist: list[str]) -> tuple[list[dict[str, Any]], str]:
    """Read Stata's exported `input.csv` once and freeze `model_input.json`."""
    frozen_path = run_dir / "model_input.json"
    if frozen_path.exists():
        records = json.loads(frozen_path.read_text(encoding="utf-8"))
        return records, sha256_file(frozen_path)

    csv_path = run_dir / "input.csv"
    if not csv_path.is_file():
        raise FileNotFoundError(f"Missing input CSV: {csv_path}")

    records: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "_decider_rowid" not in reader.fieldnames:
            raise ValueError("input.csv must contain a `_decider_rowid` column.")
        missing_cols = [v for v in varlist if v not in reader.fieldnames]
        if missing_cols:
            raise ValueError(f"input.csv missing expected columns: {missing_cols}")
        for row in reader:
            rid = int(row["_decider_rowid"])
            if rid in seen_ids:
                raise ValueError(f"Duplicate _decider_rowid in input.csv: {rid}")
            seen_ids.add(rid)
            fields = {v: (row[v] if row[v] is not None else "") for v in varlist}
            records.append({"row_id": rid, "fields": fields})

    records.sort(key=lambda r: r["row_id"])
    frozen_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return records, sha256_file(frozen_path)


# ---------------------------------------------------------------------------
# Inference backends
# ---------------------------------------------------------------------------

def _find_vendor_dir(vendor_dir: str | None) -> Path | None:
    candidates: list[Path] = []
    if vendor_dir:
        candidates.append(Path(vendor_dir))
    env_dir = os.environ.get("NEOJEV_VENDOR_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(MANAGED_VENDOR_DIR)
    candidates.append(Path(__file__).resolve().parent / "vendor" / "neojev")

    for directory in candidates:
        if (directory / "decider.py").is_file():
            return directory
    return None


def _load_decider_class(vendor_dir: str | None) -> Any:
    found = _find_vendor_dir(vendor_dir)
    if found is not None:
        decider_py = found / "decider.py"
        dir_str = str(found)
        if dir_str not in sys.path:
            sys.path.insert(0, dir_str)
        spec = importlib.util.spec_from_file_location("_neojev_vendor_decider", decider_py)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.Decider

    # Fallback: maybe decider is already importable on sys.path.
    try:
        from decider import Decider  # type: ignore
        return Decider
    except ImportError as exc:
        raise RuntimeError(
            "Could not locate NeoJev's decider.py. Run `decider setup, local` "
            "(installs into ~/.decider/vendor/neojev), pass vendordir(...), or set NEOJEV_VENDOR_DIR."
        ) from exc


class NeoJevBackend:
    """Local Apple Silicon MLX backend via NeoJev's Decider."""

    def __init__(self, config: dict[str, Any], run_dir: Path) -> None:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        from huggingface_hub import snapshot_download  # type: ignore

        Decider = _load_decider_class(config.get("vendor_dir"))
        self.path = snapshot_download(config["model"], local_files_only=True)
        self.model = Decider(self.path, token_budget=config["token_budget"])
        self._lock = threading.Lock()
        pkgs = {}
        for pkg in ("mlx", "mlx-lm", "numpy", "huggingface-hub", "pillow"):
            try:
                pkgs[pkg] = importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                pass
        self.metadata: dict[str, Any] = {
            "backend": "neojev",
            "requested_model": config["model"],
            "returned_models": [config["model"]],
            "model_snapshot": self.path,
            "packages": pkgs,
            "successful_requests": 0,
            "reported_cost_usd": None,
            "requests_with_reported_cost": 0,
        }

    def state_tokens(self, state: str) -> int | None:
        return len(self.model._enc(state))

    def ask(self, state: str, questions: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        reserved = len(self.model._enc(self.model.tail)) + len(self.model._enc("Q1:")) + 4
        if self.model._cap(state, reserved) != state:
            raise ValueError(
                "Input would be truncated by local NeoJev. Increase tokenbudget()."
            )
        result = self.model.ask(state, questions, cache_schema=True)
        with self._lock:
            self.metadata["successful_requests"] += 1
        return result


class OpenRouterBackend:
    """Hosted TypeSafe Jev backend via OpenRouter's Decisions API (stdlib only)."""

    def __init__(self, config: dict[str, Any], run_dir: Path) -> None:
        if not config.get("allow_cloud", False):
            raise ValueError(
                "Cloud transmission is disabled. Specify allowcloud when using "
                "backend(openrouter) only if this dataset may leave your machine."
            )
        self.key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not self.key:
            raise ValueError(
                "OPENROUTER_API_KEY is not set in the environment. Set it before "
                "running backend(openrouter)."
            )
        self.model = config["model"]
        self.timeout = int(config.get("timeout_seconds", 60))
        self.log_path = run_dir / "api_calls.jsonl"
        self.attempt_log_path = run_dir / "api_attempts.jsonl"
        self._lock = threading.Lock()
        self.metadata: dict[str, Any] = {
            "backend": "openrouter",
            "requested_model": self.model,
            "endpoint": OPENROUTER_ENDPOINT,
            "model_snapshot": None,
            "returned_models": [],
            "providers": [],
            "successful_requests": 0,
            "reported_cost_usd": 0.0,
            "requests_with_reported_cost": 0,
        }
        # Restore usage totals on resume from existing successful call log.
        if self.log_path.exists():
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._record_usage(json.loads(line))

    def _record_usage(self, result: dict[str, Any]) -> None:
        self.metadata["successful_requests"] += 1
        for field, val in (("returned_models", result.get("model")),
                           ("providers", result.get("provider"))):
            if val and val not in self.metadata[field]:
                self.metadata[field].append(val)
        usage = result.get("usage") or {}
        if usage.get("cost") is not None:
            self.metadata["reported_cost_usd"] += float(usage["cost"])
            self.metadata["requests_with_reported_cost"] += 1

    def _log_attempt(self, started: float, attempt: int, outcome: str,
                     context: dict[str, Any], **details: Any) -> dict[str, Any]:
        entry = {
            "finished_at_utc": utc_now_iso(),
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "attempt": attempt + 1,
            "outcome": outcome,
            "timeout_seconds": self.timeout,
            **context,
            **details,
        }
        with self._lock:
            with self.attempt_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def state_tokens(self, state: str) -> int | None:
        return None

    def ask(self, state: str, questions: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        converted: dict[str, Any] = {}
        option_maps: dict[str, dict[str, str]] = {}
        original_types: dict[str, str] = {}

        for name, spec in questions.items():
            qtype = spec["type"]
            original_types[name] = qtype
            if qtype in ("choice", "score"):
                opts = spec["options"] if qtype == "choice" else spec["levels"]
                # Keep stable option keys when choice order reverses.
                sorted_opts = sorted(opts)
                mapping = {f"option_{sorted_opts.index(label)}": label for label in opts}
                option_maps[name] = mapping
                converted[name] = {
                    "type": "choice",
                    "instructions": spec["prompt"],
                    "criteria": mapping,
                }
            elif qtype == "noul":
                converted[name] = {
                    "type": "noul",
                    "instructions": "Is this statement true? " + spec["statement"],
                }
            else:
                raise ValueError(f"Unsupported question type: {qtype!r}")

        payload = {"model": self.model, "state": state, "questions": converted}
        request = urllib.request.Request(
            OPENROUTER_ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        result: dict[str, Any] | None = None
        timing: dict[str, Any] = {}
        for attempt in range(2):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    result = json.load(resp)
                timing = self._log_attempt(started, attempt, "success", context)
                break
            except urllib.error.HTTPError as err:
                self._log_attempt(started, attempt, "http_error", context, http_status=err.code)
                if attempt == 0 and (err.code == 429 or err.code >= 500):
                    time.sleep(2.0)
                    continue
                raise RuntimeError(
                    f"OpenRouter HTTP {err.code}. Check API key, balance, and model availability."
                ) from None
            except (urllib.error.URLError, TimeoutError) as err:
                cause = getattr(err, "reason", err)
                category = "timeout" if isinstance(cause, TimeoutError) else "connection_error"
                timing = self._log_attempt(
                    started, attempt, category, context, error_type=type(cause).__name__
                )
                raise RuntimeError(
                    f"OpenRouter {category} after {timing['elapsed_seconds']:.1f}s. "
                    "See api_attempts.jsonl; use `decider resume` instead of restarting."
                ) from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._log_attempt(started, attempt, "invalid_response", context)
                raise RuntimeError("OpenRouter returned an unreadable response.") from None

        assert result is not None
        with self._lock:
            log_entry = {k: result.get(k) for k in ("id", "model", "provider", "usage", "answers")}
            log_entry["call_number"] = self.metadata["successful_requests"] + 1
            log_entry["question_names"] = list(questions)
            log_entry.update(timing)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            self._record_usage(result)

        normalized: dict[str, Any] = {}
        for name, spec in questions.items():
            answer = result["answers"][name]
            qtype = original_types[name]
            expected_api_type = "choice" if qtype in ("choice", "score") else "noul"
            if answer.get("type") != expected_api_type:
                raise ValueError(f"OpenRouter returned wrong type {answer.get('type')!r}.")
            if qtype in ("choice", "score"):
                mapping = option_maps[name]
                if set(answer["probabilities"]) != set(mapping):
                    raise ValueError("OpenRouter returned unexpected choice keys.")
                probs = {label: float(answer["probabilities"][key]) for key, label in mapping.items()}
                probs_list = list(probs.values())
                value = max(probs, key=probs.get)  # type: ignore[arg-type]
                item: dict[str, Any] = {
                    "probs": probs,
                    "probs_list": probs_list,
                    "value": value,
                    "confidence": probs[value],
                }
                if qtype == "score":
                    item["expected_index"] = sum(idx * p for idx, p in enumerate(probs_list))
                normalized[name] = item
            else:
                yes = float(answer["noul"])
                normalized[name] = {
                    "probs": {"Yes": yes, "No": 1.0 - yes},
                    "probs_list": [yes, 1.0 - yes],
                    "value": yes,
                    "confidence": max(yes, 1.0 - yes),
                }
        return normalized


class MockBackend:
    """Deterministic offline backend for unit tests and Stata integration tests."""

    def __init__(self, config: dict[str, Any], run_dir: Path) -> None:
        self._lock = threading.Lock()
        self.metadata: dict[str, Any] = {
            "backend": "mock",
            "requested_model": config["model"],
            "returned_models": [config["model"]],
            "model_snapshot": None,
            "successful_requests": 0,
            "reported_cost_usd": None,
            "requests_with_reported_cost": 0,
        }

    def state_tokens(self, state: str) -> int | None:
        return len(state.split())

    def ask(self, state: str, questions: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.metadata["successful_requests"] += 1
        out: dict[str, Any] = {}
        for name, spec in questions.items():
            qtype = spec["type"]
            if qtype == "noul":
                seed_text = f"{state}||{spec['statement']}"
                h = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16)
                # Map to [0.10, 0.95] deterministically
                yes = round(0.10 + (h % 851) / 1000.0, 6)
                out[name] = {
                    "probs": {"Yes": yes, "No": round(1.0 - yes, 6)},
                    "probs_list": [yes, round(1.0 - yes, 6)],
                    "value": yes,
                    "confidence": max(yes, round(1.0 - yes, 6)),
                }
            else:
                opts = spec["options"] if qtype == "choice" else spec["levels"]
                # Include option order slightly when state contains "[UNSTABLE]" for testing reversecheck
                order_salt = "|".join(opts) if "[UNSTABLE]" in state else ""
                raw_scores = []
                for opt in opts:
                    digest = hashlib.sha256(f"{state}||{spec['prompt']}||{opt}||{order_salt}".encode("utf-8")).digest()
                    # Sharpen slightly so most mock rows are decisive
                    val = int.from_bytes(digest[:4], "big") / float(2**32)
                    raw_scores.append(math.exp(3.5 * val))
                total = sum(raw_scores)
                probs_list = [s / total for s in raw_scores]
                probs = {opt: p for opt, p in zip(opts, probs_list)}
                best_idx = max(range(len(opts)), key=lambda i: probs_list[i])
                item: dict[str, Any] = {
                    "probs": probs,
                    "probs_list": probs_list,
                    "value": opts[best_idx],
                    "confidence": probs_list[best_idx],
                }
                if qtype == "score":
                    item["expected_index"] = sum(i * p for i, p in enumerate(probs_list))
                out[name] = item
        return out


def create_backend(config: dict[str, Any], run_dir: Path) -> Any:
    backend = config["backend"]
    if backend == "neojev":
        return NeoJevBackend(config, run_dir)
    if backend == "openrouter":
        return OpenRouterBackend(config, run_dir)
    if backend == "mock":
        return MockBackend(config, run_dir)
    raise ValueError(f"Unsupported backend: {backend!r}")


def checked_ask(model: Any, state: str, questions: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Validate probability distribution invariants across all backends."""
    result = model.ask(state, questions, context)
    answer = result[next(iter(questions))]
    probs = answer["probs_list"]
    if not all(math.isfinite(p) and 0.0 <= p <= 1.0 for p in probs):
        raise ValueError("Model returned an invalid probability outside [0, 1].")
    if not math.isclose(sum(probs), 1.0, abs_tol=1e-4):
        raise ValueError(f"Returned probabilities sum to {sum(probs):.6f}, not 1.0.")
    return answer


# ---------------------------------------------------------------------------
# Row scoring logic
# ---------------------------------------------------------------------------

def score_columns(config: dict[str, Any]) -> list[str]:
    qtype = config["type"]
    cols = ["row_id", "status"]
    if qtype == "noul":
        cols.append("prob")
    else:
        cols.append("class_idx")
        for idx in range(1, len(config["options"]) + 1):
            cols.append(f"p{idx}")
        cols.extend(["top_p", "margin"])
        if qtype == "score":
            cols.append("expected")
        if config["reverse_check"]:
            cols.extend(["rev_class_idx", "order_stable"])
    cols.extend(["review", "review_reason", "state_tokens"])
    return cols


def build_state(record: dict[str, Any], varlist: list[str]) -> tuple[str | None, list[str]]:
    fields = record["fields"]
    missing = [v for v in varlist if not str(fields.get(v, "")).strip()]
    if missing:
        return None, missing
    if len(varlist) == 1:
        return str(fields[varlist[0]]).strip(), []
    payload = {v: str(fields[v]).strip() for v in varlist}
    return json.dumps(payload, ensure_ascii=False), []


def score_one_record(model: Any, record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    qtype = config["type"]
    varlist = config["varlist"]
    options = config["options"]
    row: dict[str, Any] = {col: "" for col in score_columns(config)}
    row["row_id"] = record["row_id"]

    state, missing = build_state(record, varlist)
    if state is None:
        row["status"] = "missing_text"
        row["review"] = 1
        row["review_reason"] = "Missing " + ", ".join(missing)
        return row

    row["status"] = "scored"
    toks = model.state_tokens(state)
    row["state_tokens"] = "" if toks is None else toks
    qkey = config["prefix"] or "decision"

    if qtype == "noul":
        ctx = {"row_id": record["row_id"], "choice_order": "original"}
        ans = checked_ask(
            model, state, {qkey: {"type": "noul", "statement": config["prompt_text"]}}, ctx
        )
        prob = float(ans["value"])
        row["prob"] = prob
        flagged = prob < config["threshold"]
        row["review"] = 1 if flagged else 0
        row["review_reason"] = "Below probability threshold" if flagged else ""
        return row

    # choice or score
    opt_key = "options" if qtype == "choice" else "levels"
    orders = [("original", list(options))]
    if config["reverse_check"]:
        orders.append(("reversed", list(reversed(options))))

    answers = []
    for order_name, ordered_opts in orders:
        ctx = {"row_id": record["row_id"], "choice_order": order_name}
        spec = {"type": qtype, opt_key: ordered_opts, "prompt": config["prompt_text"]}
        answers.append(checked_ask(model, state, {qkey: spec}, ctx))

    orig = answers[0]
    row["class_idx"] = options.index(orig["value"]) + 1
    probs_in_order = [float(orig["probs"][opt]) for opt in options]
    for idx, pval in enumerate(probs_in_order, 1):
        row[f"p{idx}"] = pval

    ranked = sorted(probs_in_order, reverse=True)
    top_p = ranked[0]
    margin = ranked[0] - ranked[1]
    row["top_p"] = top_p
    row["margin"] = margin

    if qtype == "score":
        # 1-based expected level index matching Stata's 1..K value labels
        row["expected"] = sum(idx * p for idx, p in enumerate(probs_in_order, 1))

    reasons: list[str] = []
    if top_p < config["min_prob"]:
        reasons.append("Low top probability")
    if margin < config["min_margin"]:
        reasons.append("Small probability gap")

    if config["reverse_check"]:
        rev = answers[1]
        rev_idx = options.index(rev["value"]) + 1
        stable = orig["value"] == rev["value"]
        row["rev_class_idx"] = rev_idx
        row["order_stable"] = 1 if stable else 0
        if not stable:
            reasons.append("Rating changes with option order")

    row["review"] = 1 if reasons else 0
    row["review_reason"] = "; ".join(reasons)
    return row


# ---------------------------------------------------------------------------
# Locking, checkpointing, and summary output
# ---------------------------------------------------------------------------

def acquire_lock(run_dir: Path) -> Path:
    lock_path = run_dir / ".scoring.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(
            f"Run directory is locked ({lock_path}). Wait for the active scorer to "
            "finish, or remove .scoring.lock only after confirming no scorer is running."
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "started_at_utc": utc_now_iso()}) + "\n")
    return lock_path


def validate_resume_inputs(current_path: Path, records: list[dict[str, Any]],
                           varlist: list[str]) -> None:
    """Require the original text at every saved position, including missing text.

    Compare exact exported strings rather than normalized model states: even an
    edit that leaves the model state unchanged should require deliberate review.
    Rows outside the original scoring sample do not participate in this check.
    """
    expected = {record["row_id"]: record["fields"] for record in records}
    with current_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not set(["_decider_rowid", *varlist]).issubset(reader.fieldnames):
            raise ValueError("Resume mismatch: original string input variables are missing or changed type.")
        matched = set()
        for row in reader:
            rid = int(row["_decider_rowid"])
            if rid in expected:
                if {v: row[v] for v in varlist} != expected[rid]:
                    raise ValueError(
                        f"Resume mismatch at observation {rid}: input text or row order changed. "
                        "Restore the original inputs and order before resuming."
                    )
                matched.add(rid)
        if matched != set(expected):
            raise ValueError("Resume mismatch: original observations are missing from the active dataset.")


def load_existing_scores(scores_path: Path, valid_row_ids: set[int],
                         config: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Reject damaged checkpoints; never silently discard or rescore a row."""
    if not scores_path.exists():
        return {}
    # Every writerow ends in a newline. A missing terminator may indicate a
    # write interrupted in the final field, even if CSV parsing accepts it.
    with scores_path.open("rb") as handle:
        handle.seek(0, 2)
        if handle.tell():
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                raise ValueError("Incomplete scores.csv checkpoint: missing final newline; no rows were rescored.")
    completed: dict[int, dict[str, Any]] = {}
    columns = score_columns(config)
    with scores_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames != columns:
            raise ValueError("Invalid scores.csv checkpoint header; no rows were rescored.")
        try:
            for row in reader:
                # DictReader fills absent trailing fields with None. Empty strings
                # are legitimate only for fields that this row's status permits.
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("incomplete row")
                rid = int(row["row_id"])
                if rid not in valid_row_ids or rid in completed:
                    raise ValueError("unknown or duplicate row_id")
                if row["status"] not in ("scored", "missing_text") or row["review"] not in ("0", "1"):
                    raise ValueError("invalid status or review flag")
                if row["review"] == "1" and not row["review_reason"]:
                    raise ValueError("missing review reason")
                numeric = [c for c in columns if c not in
                           ("row_id", "status", "review", "review_reason", "state_tokens")]
                if row["status"] == "missing_text":
                    if row["review"] != "1" or any(row[c] != "" for c in numeric):
                        raise ValueError("invalid missing-text row")
                else:
                    values = {c: float(row[c]) for c in numeric}
                    if not all(math.isfinite(v) for v in values.values()):
                        raise ValueError("nonfinite score")
                    if config["type"] == "noul":
                        probabilities = [values["prob"]]
                    else:
                        k = len(config["options"])
                        probabilities = [values[f"p{i}"] for i in range(1, k + 1)]
                        if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-4):
                            raise ValueError("probabilities do not sum to one")
                        for name in ("class_idx", "rev_class_idx"):
                            if name in values and values[name] not in range(1, k + 1):
                                raise ValueError("invalid class index")
                        if "order_stable" in values and values["order_stable"] not in (0, 1):
                            raise ValueError("invalid order stability flag")
                    if any(not 0 <= v <= 1 for v in probabilities):
                        raise ValueError("probability outside [0, 1]")
                if row["state_tokens"] and int(row["state_tokens"]) < 0:
                    raise ValueError("invalid token count")
                completed[rid] = row
        except (ValueError, csv.Error) as exc:
            raise ValueError(
                f"Invalid or incomplete scores.csv checkpoint near line {reader.line_num}: {exc}. "
                "No rows were rescored; inspect the checkpoint before retrying."
            ) from None
    return completed


def write_summary(run_dir: Path, config: dict[str, Any], backend_meta: dict[str, Any],
                  input_sha256: str, rows: list[dict[str, Any]], resumed: bool) -> None:
    n_total = len(rows)
    n_scored = sum(1 for r in rows if r["status"] == "scored")
    n_missing = sum(1 for r in rows if r["status"] == "missing_text")
    n_flagged = sum(1 for r in rows if str(r["review"]) == "1")
    returned_models = backend_meta.get("returned_models") or []
    returned_model = returned_models[0] if len(returned_models) == 1 else "; ".join(returned_models)
    cost = backend_meta.get("reported_cost_usd")

    metadata = {
        "finished_at_utc": utc_now_iso(),
        "resumed": resumed,
        "input_sha256": input_sha256,
        "prompt_sha256": config["prompt_sha256"],
        "prompt_file": config["prompt_file"],
        "type": config["type"],
        "prefix": config["prefix"],
        "varlist": config["varlist"],
        "options": config["options"],
        "reverse_check": config["reverse_check"],
        "workers": config["workers"],
        "n_total": n_total,
        "n_scored": n_scored,
        "n_missing": n_missing,
        "n_flagged": n_flagged,
        "backend_metadata": backend_meta,
    }
    (run_dir / "model_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Flat key=value summary for effortless, lossless parsing in Stata.
    summary_lines = [
        f"backend={config['backend']}",
        f"model={config['model']}",
        f"returned_model={returned_model}",
        f"prompt_sha256={config['prompt_sha256']}",
        f"prompt_file={config['prompt_file']}",
        f"input_sha256={input_sha256}",
        f"type={config['type']}",
        f"prefix={config['prefix']}",
        f"varlist={' '.join(config['varlist'])}",
        f"reverse_check={1 if config['reverse_check'] else 0}",
        f"min_prob={config['min_prob']}",
        f"min_margin={config['min_margin']}",
        f"threshold={config['threshold']}",
        f"workers={config['workers']}",
        f"n_options={len(config['options'])}",
        f"n_requests={backend_meta.get('successful_requests', 0)}",
        f"cost_usd={'' if cost is None else cost}",
    ]
    (run_dir / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

def run_scoring(run_dir: Path, resume: bool = False,
                timeout_override: int | None = None,
                workers_override: int | None = None,
                current_input: Path | None = None) -> None:
    run_dir = run_dir.resolve()
    config = load_or_freeze_config(run_dir)
    if timeout_override is not None:
        config["timeout_seconds"] = timeout_override
    if workers_override is not None:
        if workers_override < 1:
            raise ValueError("workers() must be a positive integer.")
        if config["backend"] == "neojev" and workers_override > 1:
            raise ValueError("workers() > 1 is not supported for backend(neojev).")
        config["workers"] = workers_override

    records, input_sha256 = load_or_freeze_inputs(run_dir, config["varlist"])
    if resume and current_input is not None:
        validate_resume_inputs(current_input, records, config["varlist"])
    valid_ids = {r["row_id"] for r in records}
    scores_path = run_dir / "scores.csv"
    cols = score_columns(config)

    if scores_path.exists() and not resume:
        raise RuntimeError(
            f"{scores_path} already exists. Refusing to overwrite; use `decider resume` "
            "or choose a new rundir()."
        )

    lock_path = acquire_lock(run_dir)
    try:
        completed = load_existing_scores(scores_path, valid_ids, config) if resume else {}
        pending = [r for r in records if r["row_id"] not in completed]

        model = create_backend(config, run_dir)
        write_lock = threading.Lock()

        write_header = not scores_path.exists()
        with scores_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=cols)
            if write_header:
                writer.writeheader()
                handle.flush()

            def _process(rec: dict[str, Any]) -> dict[str, Any]:
                scored_row = score_one_record(model, rec, config)
                with write_lock:
                    writer.writerow(scored_row)
                    handle.flush()
                    completed[rec["row_id"]] = scored_row
                return scored_row

            if pending:
                workers = config["workers"]
                if workers == 1:
                    for rec in pending:
                        _process(rec)
                else:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [pool.submit(_process, rec) for rec in pending]
                        for fut in as_completed(futures):
                            fut.result()

        # Rewrite scores.csv in deterministic row_id order after all rows succeed.
        ordered_rows = [completed[r["row_id"]] for r in records]
        tmp_path = run_dir / "scores.csv.tmp"
        with tmp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=cols)
            writer.writeheader()
            writer.writerows(ordered_rows)
        tmp_path.replace(scores_path)

        write_summary(run_dir, config, model.metadata, input_sha256, ordered_rows, resume)
    finally:
        if lock_path.exists():
            lock_path.unlink()


# ---------------------------------------------------------------------------
# Diagnostics & guided setup (`decider check` and `decider setup`)
# ---------------------------------------------------------------------------

def _managed_venv_python() -> Path:
    if platform.system() == "Windows":
        return MANAGED_VENV / "Scripts" / "python.exe"
    return MANAGED_VENV / "bin" / "python"


def _probe_local_env(python_exe: str, model_id: str) -> dict[str, Any]:
    """Inspect MLX, GPU, and cached model weights inside the target Python."""
    probe_code = (
        "import json, os\n"
        "os.environ['HF_HUB_OFFLINE'] = '1'\n"
        "os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'\n"
        "out = {'has_mlx': False, 'has_gpu': False, 'device': '', 'has_weights': False}\n"
        "try:\n"
        "    import mlx.core as mx, mlx_lm\n"
        "    out['has_mlx'] = True\n"
        "    dev = str(mx.default_device())\n"
        "    out['device'] = dev\n"
        "    out['has_gpu'] = 'gpu' in dev.lower()\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    from huggingface_hub import snapshot_download\n"
        "    snapshot_download(os.environ['_DECIDER_PROBE_MODEL'], local_files_only=True)\n"
        "    out['has_weights'] = True\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps(out))\n"
    )
    env = os.environ.copy()
    env["_DECIDER_PROBE_MODEL"] = model_id
    try:
        proc = subprocess.run(
            [python_exe, "-c", probe_code],
            capture_output=True, text=True, timeout=20, env=env, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    return {"has_mlx": False, "has_gpu": False, "device": "", "has_weights": False}


def run_check(output_path: Path | None = None, vendor_dir: str | None = None,
              model_id: str = DEFAULT_NEOJEV_MODEL,
              local_python_override: str | None = None) -> dict[str, Any]:
    """Check readiness for both remote (OpenRouter) and local (NeoJev) backends."""
    os_name = platform.system()
    arch = platform.machine()
    apple_silicon = (os_name == "Darwin" and arch.lower() in ("arm64", "aarch64"))
    py_ver = platform.python_version()
    py_ok = sys.version_info >= (3, 10)
    has_api_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    remote_ready = py_ok and has_api_key

    managed_py = _managed_venv_python()
    local_py = local_python_override or (str(managed_py) if managed_py.is_file() else sys.executable)
    found_vendor = _find_vendor_dir(vendor_dir)
    has_vendor = found_vendor is not None

    if apple_silicon:
        probe = _probe_local_env(local_py, model_id)
    else:
        probe = {"has_mlx": False, "has_gpu": False, "device": "", "has_weights": False}

    local_ready = apple_silicon and probe["has_mlx"] and probe["has_gpu"] and has_vendor and probe["has_weights"]

    info = {
        "os": os_name,
        "arch": arch,
        "apple_silicon": 1 if apple_silicon else 0,
        "python_exe": sys.executable,
        "python_version": py_ver,
        "local_python": local_py,
        "has_api_key": 1 if has_api_key else 0,
        "remote_ready": 1 if remote_ready else 0,
        "has_mlx": 1 if probe["has_mlx"] else 0,
        "has_gpu": 1 if probe["has_gpu"] else 0,
        "device": probe["device"],
        "has_vendor": 1 if has_vendor else 0,
        "vendor_dir": str(found_vendor) if found_vendor else "",
        "has_weights": 1 if probe["has_weights"] else 0,
        "local_ready": 1 if local_ready else 0,
        "model": model_id,
    }
    if output_path is not None:
        lines = [f"{k}={v}" for k, v in info.items()]
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return info


def run_setup_openrouter() -> None:
    os_name = platform.system()
    if sys.version_info < (3, 10):
        raise RuntimeError(f"Python 3.10+ is required; found {platform.python_version()}.")
    has_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    print(f"Python {platform.python_version()} OK ({sys.executable}).")
    print("OpenRouter uses only the Python standard library; no packages need to be installed.")
    if has_key:
        print("OPENROUTER_API_KEY is detected in the environment. Remote backend is ready.")
    else:
        print("OPENROUTER_API_KEY is NOT currently visible to Stata.")
        if os_name == "Darwin":
            print("  macOS (GUI Stata): run in Terminal, then restart Stata:")
            print("    launchctl setenv OPENROUTER_API_KEY \"sk-or-v1-...\"")
            print("  macOS (Terminal / batch Stata): add to ~/.zshrc:")
            print("    export OPENROUTER_API_KEY=\"sk-or-v1-...\"")
        elif os_name == "Windows":
            print("  Windows: run in Command Prompt, then restart Stata:")
            print("    setx OPENROUTER_API_KEY \"sk-or-v1-...\"")
        else:
            print("  Linux: export OPENROUTER_API_KEY=\"sk-or-v1-...\" before launching Stata.")


def run_setup_local(model_id: str = DEFAULT_NEOJEV_MODEL, download_model: bool = False) -> None:
    os_name = platform.system()
    arch = platform.machine()
    if not (os_name == "Darwin" and arch.lower() in ("arm64", "aarch64")):
        raise RuntimeError(
            f"Local NeoJev requires an Apple Silicon Mac (Darwin arm64); detected {os_name} {arch}. "
            "Use backend(openrouter) on this machine."
        )

    DECIDER_HOME.mkdir(parents=True, exist_ok=True)
    venv_py = _managed_venv_python()
    uv_bin = shutil.which("uv")

    if not venv_py.is_file():
        print(f"Creating managed Python environment at {MANAGED_VENV} ...")
        if uv_bin:
            subprocess.run([uv_bin, "venv", "--python", "3.12", str(MANAGED_VENV)], check=True)
        else:
            subprocess.run([sys.executable, "-m", "venv", str(MANAGED_VENV)], check=True)

    print("Installing pinned MLX and NeoJev dependencies ...")
    if uv_bin:
        subprocess.run(
            [uv_bin, "pip", "install", "--python", str(venv_py), *NEOJEV_DEPENDENCIES],
            check=True,
        )
    else:
        subprocess.run([str(venv_py), "-m", "pip", "install", *NEOJEV_DEPENDENCIES], check=True)

    MANAGED_VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading NeoJev helper scripts into {MANAGED_VENDOR_DIR} ...")
    dl_helpers = (
        "from huggingface_hub import hf_hub_download\n"
        f"repo = {model_id!r}\n"
        f"dest = {str(MANAGED_VENDOR_DIR)!r}\n"
        f"for fname in {NEOJEV_HELPER_FILES!r}:\n"
        "    hf_hub_download(repo_id=repo, filename=fname, local_dir=dest)\n"
        "print('Helper scripts ready.')\n"
    )
    subprocess.run([str(venv_py), "-c", dl_helpers], check=True)

    if download_model:
        print(f"Downloading model weights for {model_id} (~13.4 GB) ...")
        dl_weights = (
            "from huggingface_hub import snapshot_download\n"
            f"path = snapshot_download({model_id!r})\n"
            "print('Model cached at:', path)\n"
        )
        subprocess.run([str(venv_py), "-c", dl_weights], check=True)
    else:
        print("Skipped downloading the ~13.4 GB model weights.")
        print("Run `decider setup, local downloadmodel` when ready to cache the weights.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stata decider scoring engine")
    parser.add_argument("run_dir", nargs="?", default=None, help="Path to the run directory")
    parser.add_argument("--current-input", type=Path, help="Active Stata input snapshot for resume validation")
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted run")
    parser.add_argument("--timeout", type=int, default=None, help="Override request timeout (seconds)")
    parser.add_argument("--workers", type=int, default=None, help="Override worker count")
    parser.add_argument("--check", action="store_true", help="Check local and remote backend readiness")
    parser.add_argument("--check-out", default=None, help="Write key=value check summary to this file")
    parser.add_argument("--vendor-dir", default=None, help="Vendor directory override for --check")
    parser.add_argument("--local-python", default=None, help="Local Python override for --check")
    parser.add_argument("--model", default=DEFAULT_NEOJEV_MODEL, help="Model ID for check/setup")
    parser.add_argument("--setup-local", action="store_true", help="Install managed local NeoJev venv & helpers")
    parser.add_argument("--download-model", action="store_true", help="Also download local NeoJev model weights")
    parser.add_argument("--setup-openrouter", action="store_true", help="Verify OpenRouter readiness")
    args = parser.parse_args(argv)
    try:
        if args.check:
            run_check(
                output_path=Path(args.check_out) if args.check_out else None,
                vendor_dir=args.vendor_dir,
                model_id=args.model,
                local_python_override=args.local_python,
            )
            return 0
        if args.setup_local:
            run_setup_local(model_id=args.model, download_model=args.download_model)
            return 0
        if args.setup_openrouter:
            run_setup_openrouter()
            return 0
        if not args.run_dir:
            raise ValueError("run_dir is required unless --check or --setup-* is specified.")
        run_scoring(
            Path(args.run_dir),
            resume=args.resume,
            timeout_override=args.timeout,
            workers_override=args.workers,
            current_input=args.current_input,
        )
        return 0
    except Exception as exc:
        print(f"decider error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
