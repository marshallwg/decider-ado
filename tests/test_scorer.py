"""Offline unit tests for _decider_score.py (no network, no MLX weights)."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import _decider_score as ds  # noqa: E402


def _write_run(
    run_dir: Path,
    *,
    qtype: str = "choice",
    backend: str = "mock",
    allow_cloud: bool = False,
    reverse_check: bool = False,
    workers: int = 1,
    options: list[str] | None = None,
    prompt: str = "Classify alignment.",
    rows: list[dict[str, str]] | None = None,
    varlist: list[str] | None = None,
) -> None:
    varlist = varlist or ["text"]
    rows = rows or [
        {"_decider_rowid": "1", "text": "Algebra workshop aligned to math goal"},
        {"_decider_rowid": "2", "text": ""},
        {"_decider_rowid": "3", "text": "General wellness seminar [UNSTABLE]"},
    ]
    (run_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    if options:
        (run_dir / "options.txt").write_text("\n".join(options) + "\n", encoding="utf-8")
    cfg_lines = [
        f"type={qtype}",
        "prefix=tst",
        f"varlist={' '.join(varlist)}",
        f"backend={backend}",
        f"allow_cloud={1 if allow_cloud else 0}",
        "model=",
        "prompt_file=",
        f"reverse_check={1 if reverse_check else 0}",
        "min_prob=0.80",
        "min_margin=0.30",
        "threshold=0.80",
        "token_budget=4096",
        "timeout_seconds=60",
        f"workers={workers}",
        "vendor_dir=",
    ]
    (run_dir / "config_raw.txt").write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")
    with (run_dir / "input.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["_decider_rowid", *varlist])
        writer.writeheader()
        writer.writerows(rows)


class DeciderScorerTests(unittest.TestCase):
    def test_choice_reversecheck_and_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                qtype="choice",
                options=["Strong", "Weak", "Unclear"],
                reverse_check=True,
                workers=2,
            )
            ds.run_scoring(run_dir)
            with (run_dir / "scores.csv").open("r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([int(r["row_id"]) for r in rows], [1, 2, 3])
            self.assertEqual(rows[0]["status"], "scored")
            self.assertEqual(rows[1]["status"], "missing_text")
            self.assertEqual(rows[1]["review"], "1")
            self.assertIn("Missing text", rows[1]["review_reason"])
            # Probabilities sum to 1 on scored rows
            for idx in (0, 2):
                psum = sum(float(rows[idx][f"p{k}"]) for k in (1, 2, 3))
                self.assertAlmostEqual(psum, 1.0, places=5)
            self.assertTrue((run_dir / "summary.txt").is_file())
            self.assertFalse((run_dir / ".scoring.lock").exists())

    def test_score_type_computes_expected_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, qtype="score", options=["Low", "Medium", "High"])
            ds.run_scoring(run_dir)
            with (run_dir / "scores.csv").open("r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            exp = float(rows[0]["expected"])
            self.assertGreaterEqual(exp, 1.0)
            self.assertLessEqual(exp, 3.0)

    def test_noul_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, qtype="noul", prompt="The title is informative.")
            ds.run_scoring(run_dir)
            with (run_dir / "scores.csv").open("r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            p = float(rows[0]["prob"])
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_resume_skips_completed_rows_out_of_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                qtype="choice",
                options=["A", "B"],
                rows=[
                    {"_decider_rowid": "1", "text": "alpha"},
                    {"_decider_rowid": "2", "text": "beta"},
                    {"_decider_rowid": "3", "text": "gamma"},
                ],
            )
            # Freeze config & inputs and simulate partial out-of-order completion (row 3 done)
            cfg = ds.load_or_freeze_config(run_dir)
            ds.load_or_freeze_inputs(run_dir, cfg["varlist"])
            cols = ds.score_columns(cfg)
            with (run_dir / "scores.csv").open("w", encoding="utf-8", newline="") as handle:
                w = csv.DictWriter(handle, fieldnames=cols)
                w.writeheader()
                w.writerow({
                    "row_id": 3, "status": "scored", "class_idx": 1,
                    "p1": 0.9, "p2": 0.1, "top_p": 0.9, "margin": 0.8,
                    "review": 0, "review_reason": "", "state_tokens": 1,
                })

            ds.run_scoring(run_dir, resume=True, workers_override=2)
            meta = json.loads((run_dir / "model_metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(meta["resumed"])
            # Only rows 1 and 2 should have been requested during resume
            self.assertEqual(meta["backend_metadata"]["successful_requests"], 2)
            with (run_dir / "scores.csv").open("r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([int(r["row_id"]) for r in rows], [1, 2, 3])
            # Row 3 kept its original pre-checkpointed probability
            self.assertAlmostEqual(float(rows[2]["p1"]), 0.9, places=6)

    def test_resume_checks_inputs_before_backend_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, options=["A", "B"])
            ds.run_scoring(run_dir)
            checkpoint = (run_dir / "scores.csv").read_bytes()
            current = run_dir / "current.csv"
            with (run_dir / "input.csv").open() as handle:
                original = list(csv.DictReader(handle))
            cases = {
                "reordered": [dict(original[2], _decider_rowid="1"), original[1],
                              dict(original[0], _decider_rowid="3")],
                "edited": [dict(original[0], text="changed"), *original[1:]],
                "missing": original[:2],
                "blank_changed": [original[0], dict(original[1], text="now filled"), original[2]],
            }
            for name, rows in cases.items():
                with self.subTest(name=name):
                    with current.open("w", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=["_decider_rowid", "text"])
                        writer.writeheader()
                        writer.writerows(rows)
                    with mock.patch.object(ds, "create_backend") as backend:
                        with self.assertRaisesRegex(ValueError, "Resume mismatch"):
                            ds.run_scoring(run_dir, resume=True, current_input=current)
                        backend.assert_not_called()
                    self.assertEqual((run_dir / "scores.csv").read_bytes(), checkpoint)
            current.write_bytes((run_dir / "input.csv").read_bytes())
            ds.run_scoring(run_dir, resume=True, current_input=current)
            self.assertEqual((run_dir / "scores.csv").read_bytes(), checkpoint)

    def test_resume_rejects_damaged_checkpoints_before_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, options=["A", "B"])
            ds.run_scoring(run_dir)
            checkpoint = run_dir / "scores.csv"
            original = checkpoint.read_text()
            lines = original.splitlines(keepends=True)
            cases = {
                "partial_row": lines[0] + "1,scored,1,0.8\n",
                "missing_terminator": original.rstrip("\n"),
                "bad_header": "row_id,status\n1,scored\n",
                "missing_probability": original.replace("1,scored,", "1,scored,,", 1),
                "unterminated_quote": lines[0] + '1,scored,"unfinished',
            }
            for name, content in cases.items():
                with self.subTest(name=name):
                    checkpoint.write_text(content)
                    with mock.patch.object(ds, "create_backend") as backend:
                        with self.assertRaisesRegex(ValueError, "checkpoint"):
                            ds.run_scoring(run_dir, resume=True)
                        backend.assert_not_called()
                    self.assertEqual(checkpoint.read_text(), content)
                    self.assertFalse((run_dir / ".scoring.lock").exists())

    def test_neojev_rejects_workers_gt_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, backend="neojev", options=["A", "B"], workers=2)
            with self.assertRaises(ValueError):
                ds.run_scoring(run_dir)

    def test_openrouter_requires_allow_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, backend="openrouter", allow_cloud=False, options=["A", "B"])
            with self.assertRaises(ValueError):
                ds.run_scoring(run_dir)

    def test_openrouter_retries_once_on_429_and_logs_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                backend="openrouter",
                allow_cloud=True,
                options=["Strong", "Weak"],
                rows=[{"_decider_rowid": "1", "text": "aligned session"}],
            )
            fake_payload = {
                "id": "req_123",
                "model": "typesafe/jev-1.14",
                "provider": "TypeSafe",
                "usage": {"cost": 0.0025},
                "answers": {
                    "tst": {
                        "type": "choice",
                        "probabilities": {"option_0": 0.85, "option_1": 0.15},
                    }
                },
            }

            class _FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps(fake_payload).encode("utf-8")

            calls = {"n": 0}

            def _fake_urlopen(req, timeout=60):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ds.urllib.error.HTTPError(
                        req.full_url, 429, "Too Many Requests", hdrs=None, fp=None
                    )
                return _FakeResp()

            with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                 mock.patch.object(ds.urllib.request, "urlopen", side_effect=_fake_urlopen), \
                 mock.patch.object(ds.time, "sleep", return_value=None):
                ds.run_scoring(run_dir)

            self.assertEqual(calls["n"], 2)
            meta = json.loads((run_dir / "model_metadata.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(meta["backend_metadata"]["reported_cost_usd"], 0.0025, places=6)
            self.assertTrue((run_dir / "api_calls.jsonl").is_file())
            self.assertTrue((run_dir / "api_attempts.jsonl").is_file())

    def test_run_check_writes_summary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "check.txt"
            with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-test"}):
                info = ds.run_check(output_path=out)
            self.assertEqual(info["has_api_key"], 1)
            self.assertEqual(info["remote_ready"], 1)
            self.assertTrue(out.is_file())
            text = out.read_text(encoding="utf-8")
            self.assertIn("remote_ready=1", text)


if __name__ == "__main__":
    unittest.main()
