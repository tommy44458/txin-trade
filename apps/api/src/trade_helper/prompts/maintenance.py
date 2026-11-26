"""Review hashes and require version changes when a policy/template changes.

This command updates only checked-in prompt metadata. It never calls a model,
edits application preferences, or opens the local application database.
"""

import json
from copy import deepcopy
from pathlib import Path

from .contracts import SUPPORTED_LOCALES, SUPPORTED_TASKS, content_hash, machine_contract
from .registry import read_skills, validate_catalog


def reviewed_manifest(directory: Path, *, policy_version: str | None = None,
                      en_version: str | None = None, zh_version: str | None = None,
                      reviewed_zh_policy: str | None = None) -> dict:
    def read(path):
        return json.loads((directory / path).read_text(encoding="utf-8"))

    def read_text(path):
        return (directory / path).read_text(encoding="utf-8")

    original = read("manifest.json")
    proposed = deepcopy(original)
    catalogs = {locale: read(f"{locale}/policies.json") for locale in SUPPORTED_LOCALES}
    tasks = read("shared/task_sections.json")
    questions = read("shared/news_questions.json")
    terminology = read("shared/terminology.json")
    skills = read_skills(read, read_text)
    contracts_hash = content_hash({task: machine_contract(task) for task in SUPPORTED_TASKS})
    question_changed = content_hash(questions) != original["news_questions_sha256"]
    if question_changed and tasks["news_classification"]["prompt_version"] == original[
        "task_versions"
    ]["news_classification"]["prompt_version"]:
        raise ValueError("Changed Jev questions require a new classification question version")
    if contracts_hash != original["machine_contracts_sha256"] and all(
        tasks[task]["schema_version"] == original["task_versions"][task]["schema_version"]
        for task in SUPPORTED_TASKS
    ):
        raise ValueError("Changed machine contracts require a new schema version")
    ids = set(catalogs["en-US"])
    if ids != set(catalogs["zh-TW"]):
        raise ValueError("Both prompt languages need identical policy IDs before review")
    current_hashes = {
        identifier: {locale: content_hash(catalogs[locale][identifier]) for locale in SUPPORTED_LOCALES}
        for identifier in catalogs["en-US"]
    }
    changes = {
        locale: ids != set(original["policies"]) or any(
            original["policies"].get(identifier, {}).get(locale) != hashes[locale]
            for identifier, hashes in current_hashes.items()
        ) for locale in SUPPORTED_LOCALES
    }
    shared_changes = contracts_hash != original["machine_contracts_sha256"] or any(
        content_hash(value) != original[key]
        for key, value in (
            ("task_sections_sha256", tasks), ("news_questions_sha256", questions),
            ("terminology_sha256", terminology),
        )
    )
    new_policy = policy_version or original["policy_version"]
    # English is authoritative: any English policy content change requires a
    # new common policy version, not merely replacing the expected hash.
    if (changes["en-US"] or shared_changes) and new_policy == original["policy_version"]:
        raise ValueError("Authoritative English/shared policy changes require a new policy version")
    versions = {"en-US": en_version, "zh-TW": zh_version}
    for locale in SUPPORTED_LOCALES:
        version = versions[locale] or original["locales"][locale]["translation_version"]
        if changes[locale] and version == original["locales"][locale]["translation_version"]:
            raise ValueError(f"Changed prompt content needs a new translation version: {locale}")
        proposed["locales"][locale]["translation_version"] = version
    if new_policy != original["policy_version"]:
        if reviewed_zh_policy != new_policy:
            raise ValueError("The Traditional Chinese policies need an explicit review for the new policy version")
        proposed["locales"]["zh-TW"]["reviewed_policy_version"] = new_policy
        proposed["locales"]["en-US"]["reviewed_policy_version"] = new_policy
    proposed["policy_version"] = new_policy
    proposed["policies"] = current_hashes
    proposed["task_sections_sha256"] = content_hash(tasks)
    proposed["news_questions_sha256"] = content_hash(questions)
    proposed["terminology_sha256"] = content_hash(terminology)
    proposed["machine_contracts_sha256"] = contracts_hash
    # A strategy skill carries its own version: changed text needs a new one.
    reviewed_skills = {}
    for strategy, entry in skills["catalog"].items():
        hashes = {locale: content_hash(skills["texts"][strategy][locale]) for locale in SUPPORTED_LOCALES}
        previous = original.get("skills", {}).get(strategy)
        if previous and previous.get("version") == entry.get("version") and any(
                previous.get(locale) != hashes[locale] for locale in SUPPORTED_LOCALES):
            raise ValueError(f"Changed strategy skill needs a new skill version: {strategy}")
        reviewed_skills[strategy] = {"version": entry.get("version"), **hashes}
    proposed["skills"] = reviewed_skills
    proposed["task_versions"] = {
        task: {key: config[key] for key in ("prompt_version", "schema_version")}
        for task, config in tasks.items()
    }
    validate_catalog(manifest=proposed, catalogs=catalogs, tasks=tasks,
                     questions=questions, terminology=terminology, skills=skills)
    return proposed
