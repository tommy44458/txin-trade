"""Compile reviewable bilingual policies without duplicating trading logic."""

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files

from ..timeframes import ANALYSIS_TIMEFRAMES, TIMEFRAME_LADDER, higher_timeframes
from .contracts import (
    SUPPORTED_LOCALES,
    SUPPORTED_TASKS,
    TRADING_STRATEGIES,
    PromptBundle,
    PromptInputs,
    canonical_json,
    content_hash,
    machine_contract,
    validate_locale,
)

_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
_PARAMETERS = {
    "response_language", "response_locale", "timeframe_ladder", "timeframe_plans",
    "machine_contract", "news_questions",
}


_STRATEGY_TASKS = frozenset({"strategy_market", "strategy_positions"})


def _resource_json(path: str) -> dict:
    return json.loads(files(__package__).joinpath(path).read_text(encoding="utf-8"))


def read_skills(read_json, read_text) -> dict:
    """A trading strategy's skill: its catalog entry and its text in each prompt language."""
    catalog = read_json("skills/catalog.json")
    return {"catalog": catalog, "texts": {
        strategy: {locale: read_text(f"skills/{strategy}/{locale}.md") for locale in SUPPORTED_LOCALES}
        for strategy in catalog}}


def _resource_text(path: str) -> str:
    return files(__package__).joinpath(path).read_text(encoding="utf-8")


def _validate_skills(manifest: dict, skills: dict) -> None:
    catalog, texts = skills["catalog"], skills["texts"]
    reviewed = manifest.get("skills", {})
    if set(catalog) != set(TRADING_STRATEGIES) or set(reviewed) != set(TRADING_STRATEGIES):
        raise ValueError("Strategy skills differ from the supported trading strategies")
    for strategy, entry in catalog.items():
        version, tasks = entry.get("version"), entry.get("tasks")
        if not isinstance(version, str) or not version or version != reviewed[strategy].get("version"):
            raise ValueError(f"Strategy skill version differs from the manifest: {strategy}")
        if not isinstance(tasks, list) or not tasks or not set(tasks) <= _STRATEGY_TASKS:
            raise ValueError(f"Strategy skill tasks are invalid: {strategy}")
        for locale in SUPPORTED_LOCALES:
            text = texts[strategy].get(locale)
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Strategy skill is empty: {strategy}/{locale}")
            if content_hash(text) != reviewed[strategy].get(locale):
                raise ValueError(f"Strategy skill changed without a version review: {strategy}/{locale}")
            if not placeholders(text) <= _PARAMETERS:
                raise ValueError(f"Strategy skill has unknown parameters: {strategy}")
        if placeholders(texts[strategy]["en-US"]) != placeholders(texts[strategy]["zh-TW"]):
            raise ValueError(f"Bilingual strategy skill parameters differ: {strategy}")


def placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def validate_catalog(*, manifest: dict, catalogs: dict, tasks: dict,
                     questions: dict, terminology: dict, skills: dict | None = None) -> None:
    """Build/review validation only; this never validates a model's strategy."""
    if set(catalogs) != set(SUPPORTED_LOCALES) or set(tasks) != set(SUPPORTED_TASKS):
        raise ValueError("Prompt catalog locales or tasks are incomplete")
    expected = set(manifest["policies"])
    for locale, catalog in catalogs.items():
        if set(catalog) != expected:
            raise ValueError(f"Prompt policy IDs differ for {locale}")
        locale_version = manifest["locales"][locale]
        if locale_version["reviewed_policy_version"] != manifest["policy_version"]:
            raise ValueError(f"Prompt translation review is stale for {locale}")
        for identifier, text in catalog.items():
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Prompt policy is empty: {identifier}")
            if content_hash(text) != manifest["policies"][identifier][locale]:
                raise ValueError(f"Prompt content changed without a version review: {identifier}/{locale}")
            if not placeholders(text) <= _PARAMETERS:
                raise ValueError(f"Prompt policy has unknown parameters: {identifier}")
    for identifier in expected:
        if placeholders(catalogs["en-US"][identifier]) != placeholders(catalogs["zh-TW"][identifier]):
            raise ValueError(f"Bilingual prompt parameters differ: {identifier}")
    for task, config in tasks.items():
        ids = config["policy_ids"]
        if len(set(ids)) != len(ids) or not ids or not set(ids) <= expected:
            raise ValueError(f"Prompt task policy list is invalid: {task}")
        if not config.get("prompt_version") or not config.get("schema_version"):
            raise ValueError(f"Prompt task versions are missing: {task}")
        versions = {key: config[key] for key in ("prompt_version", "schema_version")}
        if versions != manifest["task_versions"][task]:
            raise ValueError(f"Prompt task versions differ from the manifest: {task}")
    if content_hash(tasks) != manifest["task_sections_sha256"]:
        raise ValueError("Prompt task sections changed without a version review")
    if content_hash(questions) != manifest["news_questions_sha256"]:
        raise ValueError("News classification questions changed without a version review")
    if content_hash(terminology) != manifest["terminology_sha256"]:
        raise ValueError("Prompt terminology changed without a version review")
    if content_hash({task: machine_contract(task) for task in SUPPORTED_TASKS}) != manifest[
        "machine_contracts_sha256"
    ]:
        raise ValueError("Prompt machine contracts changed without a schema/version review")
    if skills is not None:
        _validate_skills(manifest, skills)


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict, dict, dict, dict, dict, dict]:
    manifest = _resource_json("manifest.json")
    catalogs = {locale: _resource_json(f"{locale}/policies.json") for locale in SUPPORTED_LOCALES}
    tasks = _resource_json("shared/task_sections.json")
    questions = _resource_json("shared/news_questions.json")
    terminology = _resource_json("shared/terminology.json")
    skills = read_skills(_resource_json, _resource_text)
    validate_catalog(manifest=manifest, catalogs=catalogs, tasks=tasks,
                     questions=questions, terminology=terminology, skills=skills)
    return manifest, catalogs, tasks, questions, terminology, skills


def validate_registry() -> dict:
    _catalog.cache_clear()
    manifest, catalogs, tasks, _questions, _terminology, skills = _catalog()
    return {"policy_version": manifest["policy_version"], "policy_count": len(catalogs["en-US"]),
            "tasks": list(tasks), "locales": list(catalogs),
            "skills": {strategy: entry["version"] for strategy, entry in skills["catalog"].items()}}


def news_questions() -> dict:
    return deepcopy(_catalog()[3])


def task_prompt_version(task: str) -> str:
    if task not in SUPPORTED_TASKS:
        raise ValueError("Unsupported prompt task")
    return _catalog()[2][task]["prompt_version"]


def task_prompt_versions(task: str) -> list[str]:
    """The task's own prompt version and one per strategy skill it can load."""
    base = task_prompt_version(task)
    catalog = _catalog()[5]["catalog"]
    return [base, *(f"{base}:{entry['version']}" for entry in catalog.values() if task in entry["tasks"])]


def resolve_prompt(task: str, *, response_locale: str = "zh-TW",
                   prompt_locale: str = "en-US", inputs: Mapping | PromptInputs | None = None
                   ) -> PromptBundle:
    if task not in SUPPORTED_TASKS:
        raise ValueError("Unsupported prompt task")
    validate_locale(prompt_locale)
    validate_locale(response_locale)
    inputs = PromptInputs.from_mapping(inputs.__dict__ if isinstance(inputs, PromptInputs) else inputs)
    manifest, catalogs, tasks, questions, _terminology, skills = _catalog()
    config = tasks[task]
    prompt_locale = config.get("fixed_prompt_locale", prompt_locale)
    response_locale = config.get("fixed_response_locale", response_locale)
    language = {
        "en-US": {"zh-TW": "Traditional Chinese", "en-US": "English"},
        "zh-TW": {"zh-TW": "繁體中文", "en-US": "英文"},
    }[prompt_locale][response_locale]
    parameters = {
        "response_language": language, "response_locale": response_locale,
        "timeframe_ladder": " → ".join(TIMEFRAME_LADDER),
        "timeframe_plans": "; ".join(
            f"{primary}: {', '.join(higher_timeframes(primary))}" for primary in ANALYSIS_TIMEFRAMES
        ),
        "machine_contract": machine_contract(task), "news_questions": canonical_json(questions),
    }
    sections = [catalogs[prompt_locale][identifier] for identifier in config["policy_ids"]]
    template_identity = {
        "task": task, "sections": sections, "policy_ids": config["policy_ids"],
        "schema_version": config["schema_version"],
        "machine_contract": parameters["machine_contract"],
    }
    prompt_version = config["prompt_version"]
    skill = skills["catalog"].get(inputs.trading_strategy or "")
    if skill is not None and task in skill["tasks"]:
        # The chosen strategy's reviewed skill follows the shared policies, and its
        # version joins the prompt version so its results are told apart.
        sections = [*sections, skills["texts"][inputs.trading_strategy][prompt_locale]]
        prompt_version = f"{prompt_version}:{skill['version']}"
        template_identity |= {"sections": sections, "trading_strategy": inputs.trading_strategy,
                              "skill_version": skill["version"]}
    # The authoritative mapping comes only from timeframes.py. Control values
    # are passed as evidence by the caller; they cannot rewrite policy prose.
    instructions = "\n\n".join(
        _PLACEHOLDER.sub(lambda match: parameters[match.group(1)], text) for text in sections
    )
    if inputs.timeframe and task in {"strategy_market", "strategy_positions"}:
        current = {"primary_timeframe": inputs.timeframe,
                   "context_timeframes": list(higher_timeframes(inputs.timeframe))}
        instructions += "\n\n" + canonical_json({"selected_timeframe_plan": current})
    return PromptBundle.build(
        task=task, instructions=instructions, policy_version=manifest["policy_version"],
        translation_version=manifest["locales"][prompt_locale]["translation_version"],
        schema_version=config["schema_version"], prompt_version=prompt_version,
        prompt_locale=prompt_locale, response_locale=response_locale,
        template_sha256=content_hash(template_identity),
    )
