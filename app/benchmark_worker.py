"""Run in the upstream virtualenv, never import its dependencies into the API process."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def module(name: str) -> Any:
    return importlib.import_module(name)


def agentdojo_catalog(version: str) -> dict[str, Any]:
    suites = module("agentdojo.task_suite.load_suites").get_suites(version)
    if not suites:
        raise ValueError("unsupported AgentDojo benchmark version")
    cases, environments = [], {}
    for name, suite in sorted(suites.items()):
        _, (user_checks, injection_checks) = suite.check(check_injectable=False)
        candidate_probe = module("agentdojo.attacks.base_attacks").FixedJailbreakAttack(
            "{goal}",
            suite,
            module("agentdojo.agent_pipeline.ground_truth_pipeline").GroundTruthPipeline(None),
        )
        environments[name] = {
            "initial_state": suite.load_and_inject_default_environment({}).model_dump(mode="json"),
            "injection_vectors": suite.get_injection_vector_defaults(),
            "ground_truth_checks": {"users": user_checks, "injections": injection_checks},
            "visibility_check": "BaseAttack.get_injection_candidates (native runtime extraction)",
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters.model_json_schema(),
                }
                for tool in suite.tools
            ],
        }
        for user_id, task in suite.user_tasks.items():
            env = task.init_environment(suite.load_and_inject_default_environment({}))
            try:
                candidates = candidate_probe.get_injection_candidates(task)
            except ValueError:
                candidates = []
            definition = {
                "user_task_class": f"{type(task).__module__}.{type(task).__name__}",
                "ground_truth": [call.model_dump(mode="json") for call in task.ground_truth(env)],
                "utility_evaluator": "TaskSuite._check_user_task_utility",
                "security_evaluator": "TaskSuite._check_injection_task_security",
                "injection_candidates": candidates,
            }
            for injection_id, injection in [(None, None), *suite.injection_tasks.items()]:
                cases.append(
                    {
                        "id": f"agentdojo:{version}:{name}:{user_id}:{injection_id or 'clean'}",
                        "suite": name,
                        "category": "indirect_prompt_injection" if injection else "task_utility",
                        "kind": "attack" if injection else "control",
                        "user_task_id": user_id,
                        "injection_task_id": injection_id,
                        "prompt": task.PROMPT,
                        "attack_goal": injection.GOAL if injection else "",
                        "validation_error": (
                            "upstream user-task ground truth failed"
                            if not user_checks[user_id][0]
                            else "upstream injection-task ground truth failed"
                            if injection_id is not None and not injection_checks[injection_id]
                            else "no native injection candidate is reachable"
                            if injection_id is not None and not candidates
                            else None
                        ),
                        "definition": {
                            **definition,
                            "injection_task_class": (
                                f"{type(injection).__module__}.{type(injection).__name__}"
                                if injection
                                else None
                            ),
                        },
                    }
                )
    return {"cases": cases, "environment": environments}


def bounded_client(max_calls: int) -> Any:
    """Count physical model requests, including any upstream helper-model requests."""
    httpx, openai = module("httpx"), module("openai")
    calls = 0

    def before_request(_request: Any) -> None:
        nonlocal calls
        if calls >= max_calls:
            raise RuntimeError("benchmark model-call budget exhausted")
        calls += 1

    return openai.OpenAI(
        max_retries=0,
        timeout=30,
        http_client=httpx.Client(event_hooks={"request": [before_request]}, trust_env=False),
    )


def run_agentdojo(request: dict[str, Any], output: Path) -> None:
    manifest = request["manifest"]
    pipeline_module = module("agentdojo.agent_pipeline.agent_pipeline")
    llm = module("agentdojo.agent_pipeline.llms.openai_llm").OpenAILLM(
        bounded_client(request["max_calls"]), request["model"]
    )
    llm.name = request["model"]
    pipeline = pipeline_module.AgentPipeline.from_config(
        pipeline_module.PipelineConfig(
            llm=llm,
            model_id=None,
            defense=request.get("defense"),
            system_message_name=None,
            system_message=None,
        )
    )
    # Upstream attack templates recognize this provider alias for arbitrary model IDs.
    pipeline.name = f"openai-compatible:{pipeline.name}"
    benchmark = module("agentdojo.benchmark")
    logging = module("agentdojo.logging")
    load_suite = module("agentdojo.task_suite.load_suites").get_suite
    load_attack = module("agentdojo.attacks.attack_registry").load_attack
    with logging.OutputLogger(str(output)):
        for case in manifest["cases"]:
            if case.get("validation_error"):
                continue
            suite = load_suite(manifest["version"], case["suite"])
            task = suite.get_user_task_by_id(case["user_task_id"])
            if case["kind"] == "control":
                benchmark.run_task_without_injection_tasks(
                    suite, pipeline, task, output, True, manifest["version"]
                )
            else:
                attack = load_attack("important_instructions", suite, pipeline)
                benchmark.run_task_with_injection_tasks(
                    suite,
                    pipeline,
                    task,
                    attack,
                    output,
                    True,
                    [case["injection_task_id"]],
                    manifest["version"],
                )


def run_injecagent(request: dict[str, Any], source: Path, output: Path) -> None:
    """Use upstream prompt construction, two-stage simulation and output parser unchanged."""
    data = output / "data"
    data.mkdir()
    for name in ("tools.json", "attacker_simulated_responses.json"):
        shutil.copyfile(source / "data" / name, data / name)
    os.chdir(output)  # Upstream simulator writes its cache relative to cwd.
    evaluator = module("src.evaluate_prompted_agent")
    utils = module("src.utils")
    params = {"model_type": "GPT", "model_name": request["model"], "prompt_type": "InjecAgent"}
    agent = module("src.models").GPTModel(params)
    client = bounded_client(request["max_calls"])
    agent.client = client
    utils.client = client
    tool_dict = utils.get_tool_dict()
    system, user = module("src.prompts.agent_prompts").PROMPT_DICT["InjecAgent"]
    parse = module("src.output_parsing").evaluate_output_prompted
    manifest = request["manifest"]
    for suite in ("dh", "ds"):
        path = output / f"test_cases_{suite}_{manifest['version']}.json"
        with path.open("x", encoding="utf-8") as stream:
            for case in manifest["cases"]:
                if case["suite"] != suite:
                    continue
                item = dict(case["definition"])
                try:
                    item["output"] = evaluator.predict_one_case(
                        system, user, params, item, agent, tool_dict
                    )
                    parsed = parse(
                        item["output"],
                        item["Attacker Tools"][0],
                        item["Attacker Tools"],
                        item["User Tool"],
                        item["Attacker Instruction"],
                    )
                    item["eval"] = parsed["eval"]
                    if suite == "ds" and item["eval"] == "succ":
                        for key in ("Attacker Tool Parameters", "Attacker Tool Thought"):
                            item[key] = parsed[key]
                        item["Attacker Tool Response"] = utils.get_simulated_attacker_tool_response(
                            item["Attacker Tools"][0], item["Attacker Tool Parameters"], tool_dict
                        )
                        item["output Step 2"] = evaluator.predict_one_case(
                            system, user, params, item, agent, tool_dict, step_2=True
                        )
                        item["eval Step 2"] = parse(
                            item["output Step 2"],
                            item["Attacker Tools"][1],
                            item["Attacker Tools"],
                            item["User Tool"],
                            item["Attacker Instruction"],
                        )["eval"]
                except Exception as exc:  # noqa: BLE001 -- preserve upstream case failure as evidence
                    item["error"] = type(exc).__name__
                    item["eval"] = "invalid"
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                stream.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["catalog", "run"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="v1.2.2")
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path[:0] = [str(source / "src"), str(source)]
    if args.action == "catalog":
        value = agentdojo_catalog(args.version)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        return
    request = json.loads(args.request.read_text(encoding="utf-8"))
    output = args.output.resolve()
    output.mkdir(exist_ok=False)
    if request["manifest"]["benchmark"] == "agentdojo":
        run_agentdojo(request, output)
    else:
        run_injecagent(request, source, output)


if __name__ == "__main__":
    main()
