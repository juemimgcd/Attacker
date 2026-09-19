# Public Agent security benchmarks

Attacker supports the native AgentDojo environment and InjecAgent prompted-agent
protocol through `attacker benchmark`. Upstream dependencies run in a separate
Python environment. Results enter the existing SQL Run, Evidence, Finding and
JSON/Markdown report system as `benchmark_import` runs.

## Fixed sources and counts

| Benchmark | Pinned commit | Setting | Catalog size |
|---|---|---|---:|
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) | `089ed468cf3ed0322acc66b0211f26d9d90dbf60` | `v1.2.2` | 949 user/injection task pairs + 97 clean tasks |
| [InjecAgent](https://github.com/uiuc-kang-lab/InjecAgent) | `f19c9f2c79a41046eb13c03c51a24c567a8ffa07` | `base` | 1,054 attack cases |

These are separately counted catalogs, not executed results, unique vulnerability
counts or a claim of real-world coverage. The original 82 Core-authored Cases
remain the built-in regression suite. AgentDojo's current catalog differs from
its initial paper's 629 security instances. InjecAgent's `enhanced` setting is an
alternative payload variant, not another 1,054 independent scenarios.

At this pinned AgentDojo revision, workspace injection tasks 6–13 fail upstream
`suite.check(check_injectable=False)` ground-truth validation. Their 320 pairs
remain in the catalog with `validation_error` and are skipped by the runner and
excluded from scoring. This leaves **629 ground-truth-eligible attack pairs and
97 clean tasks**. Its separate `is_task_injectable` helper filters for string
content although tool responses use content-block lists. Catalog preparation
instead calls upstream `BaseAttack.get_injection_candidates`, the content-block-aware
extractor used by the actual attack, and stores reachable injection vectors for
each task. A task with no reachable vector is also not-evaluable. No upstream
code is patched.

Catalogs preserve the upstream MIT license text, source commit and a checksum of
tracked code/data. AgentDojo exports task prompts, injection goals, ground-truth
tool calls, initial environments, injection vectors, tool schemas and judge
references. InjecAgent preserves the complete source records, tool definitions
and simulated-response checksum. Upstream repositories and generated catalogs
are kept under ignored `data/`; executable upstream code is not vendored into Core.

## Prepare isolated sources

Run from the Attacker checkout. `uv` cache can be redirected with
`UV_CACHE_DIR=/tmp/attacker-uv-cache` in restricted macOS sessions.

```sh
mkdir -p data/benchmarks/upstream
git clone https://github.com/ethz-spylab/agentdojo.git data/benchmarks/upstream/agentdojo
git -C data/benchmarks/upstream/agentdojo checkout --detach 089ed468cf3ed0322acc66b0211f26d9d90dbf60
git clone https://github.com/uiuc-kang-lab/InjecAgent.git data/benchmarks/upstream/InjecAgent
git -C data/benchmarks/upstream/InjecAgent checkout --detach f19c9f2c79a41046eb13c03c51a24c567a8ffa07
uv venv data/benchmarks/.venv --python 3.12
uv pip install --python data/benchmarks/.venv/bin/python data/benchmarks/upstream/agentdojo nltk together
```

This runner uses OpenAI-compatible model APIs; no local torch/transformers backend
is needed. The upstream source is pinned; preserve an environment lock/freeze
alongside actual experiment results when comparing runs across machines.

```sh
uv run attacker benchmark catalog agentdojo \
  --source data/benchmarks/upstream/agentdojo \
  --python data/benchmarks/.venv/bin/python \
  --output data/benchmarks/agentdojo.json

uv run attacker benchmark catalog injecagent \
  --source data/benchmarks/upstream/InjecAgent \
  --output data/benchmarks/injecagent.json
```

No model credentials or model calls are needed to build either catalog. Changing
the pinned source requires a deliberate adapter/version review. Catalog and
report output paths must be new; commands do not overwrite prior experiments.

## Execute native environments

Provide `OPENAI_API_KEY` through the process environment and, for a compatible
service, `OPENAI_BASE_URL`. Credentials are not command arguments or persisted
request fields. The worker uses upstream tools in their simulated environment;
it does not connect these tools to real accounts.

```sh
uv run attacker benchmark run \
  --manifest data/benchmarks/agentdojo.json \
  --source data/benchmarks/upstream/agentdojo \
  --python data/benchmarks/.venv/bin/python \
  --model YOUR_MODEL_ID \
  --limit 4 --max-calls 40 --timeout 300 \
  --workdir data/benchmarks/dojo-baseline \
  --report data/benchmarks/dojo-baseline.md

uv run attacker benchmark run \
  --manifest data/benchmarks/injecagent.json \
  --source data/benchmarks/upstream/InjecAgent \
  --python data/benchmarks/.venv/bin/python \
  --model YOUR_MODEL_ID \
  --limit 4 --max-calls 20 --timeout 300 \
  --workdir data/benchmarks/injec-baseline \
  --report data/benchmarks/injec-baseline.md
```

The limit selects the first catalog entries, not a representative random sample.
For a scored experiment, choose explicit `--case-id` values across suites and
risk categories and keep that selection fixed. AgentDojo interleaves each clean
user task with its attack pairs; InjecAgent orders direct harm before data stealing.

The CLI requires an explicit case selection/limit and physical model-call budget.
It disables SDK retries, counts outgoing model requests and kills the worker on
the wall-time deadline. This is not a monetary/token budget. InjecAgent may call
its upstream simulator model (`gpt-4-0613`) when a simulated response is absent;
that request uses the same bounded client and is included in `--max-calls`.
If the configured service lacks that model, the case becomes incomplete/error.
Its cache writes go to the experiment directory, never the pinned source checkout.

AgentDojo uses upstream `important_instructions` and supports
`--defense repeat_user_prompt` or `--defense tool_filter`. Its task runner,
environment transitions and utility/security judges are unchanged. InjecAgent
uses upstream `predict_one_case`, `InjecAgent` prompts and output parser, including
the second data-stealing stage. Other native agent/attack protocols require an
explicit adapter; the importer does not guess their meaning.

AgentDojo pipeline labels have the form `openai-compatible:MODEL[-DEFENSE]` so its
native attack template can address model IDs outside the original model enum.
The actual model ID is still sent to the configured model endpoint.

Execution is a local benchmark workflow, not a Core Provider/Policy Gate run.
It does not claim to exercise Attacker approvals or the remote Target's internal
Policy/Approval trace. The first integration evaluates an upstream-built agent
using the selected model; an arbitrary existing HTTP Agent needs a separate
upstream pipeline adapter. Imported traces remain available as Evidence and are
never relabeled as Core-observed physical tool calls.

## Import results and interpret metrics

Existing upstream logs may be imported without making model calls:

```sh
uv run attacker benchmark import \
  --manifest data/benchmarks/agentdojo.json \
  --results PATH_TO_ONE_PIPELINE_RESULTS \
  --target EXACT_UPSTREAM_PIPELINE_NAME \
  --report data/benchmarks/imported.md
```

For InjecAgent, point `--results` at the directory containing its
`test_cases_dh_base.json` and `test_cases_ds_base.json` **JSONL result files**, not
the JSON-array source datasets. The target label is caller-supplied because the
upstream InjecAgent rows do not authenticate a model identity.

- In this pinned AgentDojo version, `security=true` on an attack Case means the
  **injection goal succeeded** (`BaseInjectionTask.security` / `TaskSuite.run_task_with_pipeline`).
  Clean tasks return a constant true in that slot; they are scored only for utility. An error
  field or missing trace/judge makes the observation error/not-evaluable even if
  upstream emitted `security=true`. A failed catalog ground-truth check also
  makes the Case not-evaluable. Results must carry the matching
  `benchmark_version`, pipeline and attack protocol; legacy unversioned logs are
  rejected instead of being attributed to the current version.
- InjecAgent data stealing succeeds only when both stages succeed. Missing second
  stages, invalid outputs and execution errors are not safe results. Its evidence
  describes predicted actions and simulated observations, not actual external
  tool execution. It does not provide a clean-task utility judge.
- Reports show attack success rate, clean utility and utility under attack with
  success/evaluable/expected counts. Missing, errored and invalid results are
  excluded from the rate denominator and separately counted. An unavailable metric
  is `N/A`, not zero. Clean-task failures are `task_failed`, not automatically
  classified as refusals or security violations.
- Each imported artifact retains a raw checksum and a redacted evidence copy.
  Checksums identify original bytes, not the redacted JSON representation. Logs
  are caller-supplied evidence, not tamper-proof proof. Keep raw artifacts locally;
  review them before sharing.

Runs are readable through the existing `/runs/{run_id}` and report endpoints
using the same database as the CLI. `target_calls=0` describes calls made by Core
during import, not the upstream model's usage. `completed_cases` counts imported
observations, including missing/error observations; use benchmark statuses and
metric denominators to assess how much was actually evaluated.

## Repair and compare

Rerun the **same manifest and selected Case IDs** into a new work directory after
changing the model or supported defense. Then compare the returned Run IDs:

```sh
uv run attacker benchmark compare SOURCE_RUN_ID REPLAY_RUN_ID
```

Comparison requires identical source/environment/protocol/Case snapshots and
conclusive results on both sides. Missing/error results return `not_comparable`
and never become `fixed`. Attack outcomes produce fixed/new/persistent labels;
new clean-task failures produce `regressed` (utility regression). The relation
is saved in the existing Replay table and appears in the replay Run report.
Regenerate/read the report through the API after attaching a comparison; an
already-exported Markdown file is a snapshot and is not overwritten.

The generic HTTP Replay endpoint cannot launch upstream benchmark processes.
Use `benchmark run` followed by `benchmark compare` for these experiments.

## Verification boundaries

Catalog generation validates real pinned upstream data. Existing Attacker tests
may validate that Run/report behavior remains compatible. Upstream ground-truth
execution can check the tool environment and evidence plumbing without a model;
it is an oracle/environment check, not a model security score. Claim an attack
success rate or defense improvement only after running an explicitly identified
model, fixed case selection and budget, with the resulting artifacts retained.
