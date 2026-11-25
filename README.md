# decider-ado

Applied text coding in Stata usually forces an awkward choice: either hand-code a small sample and give up scale, or send strings to a generative model that returns free-form prose requiring fragile post-hoc parsing. Structured decision models take a different approach: given a text state and a fixed set of options, they return a probability distribution over the permitted answers in a single pass, with no generated prose to parse.

`decider` is a Stata 16+ `rclass` package that brings structured probabilistic decisions (`choice`, ordered `score`, and binary `noul` probabilities) directly into a Stata workflow. The command is separated from any single inference provider, so a `.do` file keeps the same syntax, variable naming, and returned-results contract whether inference runs locally on an Apple Silicon Mac or remotely through a hosted API:

- `backend(openrouter)` calls OpenRouter's hosted Decisions API (default model: `typesafe/jev-1.14`). It runs on macOS and Windows, requires only Python 3.10+ with the standard library (no third-party Python packages), requires an explicit `allowcloud` option before transmitting text, and supports concurrent `workers(#)`.
- `backend(neojev)` (the default) runs [NeoJev](https://huggingface.co/dowands/NeoJev-Qwen3.8-27B-L56-MLX) locally on Apple Silicon Macs through `mlx` and `mlx-lm`. Once weights are cached, no text leaves the computer; execution is single-threaded (`workers(1)`).[^local-workers]
- `backend(mock)` provides a deterministic offline simulator for verifying pipelines, merge logic, and downstream code without model weights or API charges.

## Installation

Install the package from Stata 16 or newer:

```stata
net install decider, from("https://raw.githubusercontent.com/marshallwg/decider-ado/main") replace
```

If you cloned the repository locally, pass your local directory path inside `from(...)` instead.

After installation, run the built-in diagnostic command:

```stata
decider check
```

`decider check` inspects your operating system, CPU architecture, Python executable, `OPENROUTER_API_KEY` visibility, MLX GPU availability, helper scripts, and cached model weights; it prints a status summary, stores readiness flags in `r(remote_ready)` and `r(local_ready)`, and tells you which setup command to run next.

## Setup

Configure the backend that matches your hardware and data-use agreement; both can be configured on the same machine.

### Remote inference via OpenRouter (macOS or Windows)

Use `backend(openrouter)` on Windows, on Intel Macs, or on projects whose data-use agreement permits sending the selected string fields to OpenRouter and its Jev provider. No Python packages need to be installed.

1. Confirm that Python 3.10 or newer is available on your `PATH`. On macOS, run `python3 --version` in Terminal; on Windows, run `python --version` in Command Prompt. If Python is missing, install it from [python.org](https://www.python.org/downloads/) (on Windows, check "Add python.exe to PATH" during installation).
2. Create an account at [openrouter.ai](https://openrouter.ai/), add credits, and generate an API key (`sk-or-v1-...`).
3. Make `OPENROUTER_API_KEY` visible to Stata's environment. Do not put a real key in a `.do` file or commit it to version control.
   - On macOS (Stata GUI), run the following command in Terminal and then restart Stata:[^macos-env]
     ```bash
     launchctl setenv OPENROUTER_API_KEY "sk-or-v1-..."
     ```

     For batch or Terminal-launched Stata sessions, add `export OPENROUTER_API_KEY="sk-or-v1-..."` to `~/.zshrc`.
   - On Windows, run the following in Command Prompt and then restart Stata:
     ```cmd
     setx OPENROUTER_API_KEY "sk-or-v1-..."
     ```
4. Confirm readiness from Stata:
   ```stata
   decider setup, openrouter
   decider check
   ```

   Once `Remote (openrouter): READY` appears, `decider` is ready for cloud runs with `backend(openrouter) allowcloud`.

### Local offline inference via NeoJev (Apple Silicon Mac)

Use `backend(neojev)` when working with restricted or sensitive text that cannot leave the machine. Local inference requires an Apple Silicon Mac (M1 through M5), roughly 14 GB of free disk space for cached model weights, and roughly 15 GB of available unified memory while scoring.

#### Guided setup from Stata

Run the managed setup in two explicit steps:

```stata
* 1. Create ~/.decider/venv, install pinned MLX dependencies, and fetch NeoJev helpers
decider setup, local

* 2. Download and cache the ~13.4 GB model weights (explicit opt-in)
decider setup, local downloadmodel

* 3. Verify local readiness
decider check
```

Because `decider setup, local` installs into `~/.decider/venv` and `~/.decider/vendor/neojev`, subsequent calls to `decider ..., backend(neojev)` locate both automatically; you do not need to pass `python()` or `vendordir()`.

#### Using an existing MLX environment

If you already maintain a Python 3.12 MLX environment and a local copy of NeoJev's helper files (`decider.py`, `vision_common.py`, `vision_mlx.py`), point `decider` directly at them:

```stata
decider goal_text session_title, ///
    type(choice) prefix(align) ///
    promptfile("prompts/alignment_v2.md") ///
    options("Strong/Obvious" "Potential/Weak" "Unclear/No Evidence") ///
    backend(neojev) ///
    python("/path/to/.venv/bin/python") ///
    vendordir("/path/to/vendor/neojev") ///
    reversecheck
```

## Examples

```stata
* Multi-field classification with reversed-order stability check (OpenRouter)
decider goal_text session_title, ///
    type(choice) prefix(align) ///
    promptfile("prompts/alignment_v2.md") ///
    options("Strong/Obvious" "Potential/Weak" "Unclear/No Evidence") ///
    backend(openrouter) allowcloud workers(4) reversecheck ///
    rundir("runs/align_v2")

* Inspect returned results and option distribution matrix
return list
matlist r(dist)

* Resume an interrupted run without re-scoring completed rows
decider resume, rundir("runs/align_v2") timeout(120) replace
```

When one string variable is passed, its text is sent directly as the state; when multiple string variables are passed, they are serialized as a JSON object keyed by variable name. Observations with blank or missing input text are never sent to the model: they are retained in the sample and flagged as `missing_text`.

## How it works

`decider.ado` isolates the marked `if`/`in` sample inside a temporary Stata `frame`, exports `input.csv` (preserving `strL` text), and invokes the bundled Python engine (`_decider_score.py`) as a subprocess. Using a subprocess rather than Stata's embedded Python releases the ~15 GB MLX model memory as soon as scoring finishes and avoids changing Stata's global `python_exec` setting. Results are read back into a temporary frame and attached to the active dataset via `frlink 1:1` and `frget`, leaving the original sort order intact.

Every run freezes `config.json`, `frozen_prompt.txt`, `model_input.json`, `scores.csv`, and `model_metadata.json` inside `rundir()` (or a temporary run directory if `rundir()` is omitted). For `openrouter` runs, successful responses and call-level timing/error diagnostics are logged to `api_calls.jsonl` and `api_attempts.jsonl` without writing credentials or submitted text to the attempt log. Each completed row is flushed immediately by `row_id` behind a lock, so interrupted or parallel runs can pick up cleanly under `decider resume`.

Prompt SHA-256 hashes, backend identifiers, model identifiers, sample counts, review rates, order-stability rates, API costs, and the option distribution matrix (`r(dist)`) are returned in `r()` and recorded as variable characteristics (`char`) on every generated variable.

## Scope and limitations

- **Calibration**: Returned probabilities are model confidence values; they are not automatically calibrated probabilities for a new domain or rubric. Before relying on automated decisions operationally, validate against a labeled sample, check subgroup performance, and set review thresholds (`minprob()`, `minmargin()`, `threshold()`) empirically.
- **Order sensitivity**: Option ordering can shift model predictions. Use `reversecheck` on `choice` and `score` tasks to flag rows whose selected label changes when option order is reversed.
- **Hosted timeouts and billing**: Transient HTTP 429 and 5xx errors on OpenRouter are retried once after a short delay; network timeouts are never automatically retried because a timed-out request may already have been billed by the provider. Use `decider resume` after inspecting `api_attempts.jsonl`.
- **Hosted API stability**: OpenRouter's Decisions endpoint (`/api/alpha/decisions`) is an alpha API whose contract may change upstream.
- **Local hardware**: `backend(neojev)` runs only on Apple Silicon Macs with sufficient unified memory; Windows and Intel Mac users should use `backend(openrouter)`.

## Testing

Offline tests use `backend(mock)` and make no network calls or model loads:

```bash
python3 -m unittest discover -s tests -v
stata-mp -b do tests/test_decider.do
```

An optional live smoke test for synthetic records lives in `tests/smoke_live.do`; toggle `run_neojev` and `run_openrouter` at the top of that file before running.

[^local-workers]: MLX executes on a single Apple Silicon GPU, and `Decider`'s KV-cache is not thread-safe; spawning multiple processes would load multiple ~15 GB copies of the weights into unified memory. Local speed instead comes from keeping weights loaded once across the run and reusing the cached question prefix (`cache_schema=True`).
    
[^macos-env]: macOS GUI applications inherit their environment from `launchd` rather than your interactive shell profile (`~/.zshrc`). `launchctl setenv` sets the variable for GUI apps in the current login session.
