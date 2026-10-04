"""Typed prompt identity and unlocalized machine response contracts.

Bundle identity hashes metadata as well as instructions. Stored bundles can be
read without consulting today's registry, so queued work survives App updates.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

Locale = Literal["zh-TW", "en-US"]
PromptTask = Literal[
    "strategy_market", "strategy_positions", "discussion", "macro_interpretation",
    "macro_translation", "news_classification",
]
SUPPORTED_LOCALES = ("zh-TW", "en-US")
SUPPORTED_TASKS = (
    "strategy_market", "strategy_positions", "discussion", "macro_interpretation",
    "macro_translation", "news_classification",
)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(value: object) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def validate_locale(value: str) -> Locale:
    if value not in SUPPORTED_LOCALES:
        raise ValueError("Unsupported prompt or response locale")
    return value


@dataclass(frozen=True)
class PromptInputs:
    timeframe: str | None = None
    directional_bias: str | None = None
    risk_tolerance: str | None = None
    trading_style: str | None = None
    leverage: int | None = None

    @classmethod
    def from_mapping(cls, values: Mapping | None = None) -> "PromptInputs":
        # Analysis requests also contain private fields and IDs. Only these
        # typed control values may ever become prompt-template parameters.
        values = values or {}
        if not isinstance(values, Mapping):
            raise TypeError("Prompt inputs must be an object")
        allowed = {
            "timeframe": ("1h", "4h", "12h", "1d"),
            "directional_bias": ("bullish", "bearish"),
            "risk_tolerance": ("low", "medium", "high"),
            "trading_style": ("left", "right"),
        }
        chosen = {key: values.get(key) for key in (*allowed, "leverage")}
        for key, choices in allowed.items():
            if chosen[key] is not None and chosen[key] not in choices:
                raise ValueError(f"Unsupported prompt input: {key}")
        leverage = chosen["leverage"]
        if leverage is not None and (
            isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 125
        ):
            raise ValueError("Unsupported prompt input: leverage")
        return cls(**chosen)


@dataclass(frozen=True)
class PromptBundle:
    task: PromptTask
    instructions: str
    policy_version: str
    translation_version: str
    schema_version: str
    prompt_version: str
    prompt_locale: Locale
    response_locale: Locale
    template_sha256: str
    instructions_sha256: str
    rendered_sha256: str

    @property
    def text(self) -> str:
        return self.instructions

    def metadata(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if key != "instructions"}

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def build(cls, **fields) -> "PromptBundle":
        instructions = fields["instructions"]
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Prompt instructions must be nonempty")
        identity = fields | {"instructions_sha256": sha256(instructions.encode()).hexdigest()}
        return cls(**identity, rendered_sha256=content_hash(identity))

    @classmethod
    def from_dict(cls, raw: Mapping) -> "PromptBundle":
        if not isinstance(raw, Mapping) or set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("Stored prompt bundle is incomplete")
        if raw["task"] not in SUPPORTED_TASKS:
            raise ValueError("Stored prompt bundle has an unsupported task")
        validate_locale(raw["prompt_locale"])
        validate_locale(raw["response_locale"])
        for key in ("policy_version", "translation_version", "schema_version", "prompt_version"):
            if not isinstance(raw[key], str) or not raw[key]:
                raise ValueError("Stored prompt bundle has an incomplete version")
        for key in ("template_sha256", "instructions_sha256", "rendered_sha256"):
            value = raw[key]
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError("Stored prompt bundle has an invalid identity")
        identity = {key: value for key, value in raw.items() if key != "rendered_sha256"}
        if not isinstance(raw["instructions"], str) or not raw["instructions"].strip():
            raise ValueError("Stored prompt instructions are missing")
        if sha256(raw["instructions"].encode()).hexdigest() != raw["instructions_sha256"]:
            raise ValueError("Stored prompt instructions differ from their identity")
        if content_hash(identity) != raw["rendered_sha256"]:
            raise ValueError("Stored prompt metadata differs from its identity")
        return cls(**dict(raw))


STRATEGY_MACHINE_CONTRACT = {
    "string_fields": ["market", "levels", "strategy", "supporting_evidence", "counter_evidence"],
    "agent_stance": ["long", "short", "wait"],
    "direction_assessment": {side: {"verdict": ["reasonable", "conditional", "unsuitable"], "reason": "text"}
                             for side in ("long", "short")},
    "strategy_decision": ["candidate", "wait"],
    "entry_actions": ["open_now", "wait_for_entry", "stand_aside"],
    "entry_fields": [
        "action", "side", "entry_price", "stop_loss", "take_profit", "trigger", "trigger_rule",
        "invalidation", "invalidation_rule", "reason", "basis_level_ids",
    ],
    "invalidation_rule": {"price": "price string", "confirmation": ["close", "touch"],
                          "when": "open_now or wait_for_entry, between entry and stop; else null"},
    "trigger_rule": {"type": ["touch", "close_above", "close_below"], "price": "price string",
                     "when": "wait_for_entry only; null otherwise"},
    "position_decisions": {"position_id": {
        "decision": ["hold", "close_now"], "reason": "text",
        "exit_plan": {"invalidation": {"price": "price string", "confirmation": ["close", "touch"],
                                       "condition": "text"},
                      "protective_stop": "price string or null",
                      "take_profits": [{"price": "price string", "portion_pct": "integer 1-100 or null"}],
                      "basis_level_ids": "array of zone IDs"},
    }},
    "macro_outlook": {"stance": ["bullish", "bearish", "neutral"], "reason": "text",
                      "evidence_ids": "array of unchanged evidence IDs"},
    "evidence_tools": "array of executed evidence/tool names",
}
MACRO_MACHINE_CONTRACT = {
    "outlook": {"stance": ["bullish", "bearish", "neutral"], "summary": "text"},
    "drivers": [{"title": "text", "explanation": "text", "evidence_ids": "array of evidence IDs"}],
    "uncertainties": ["text"], "watch_next": ["text"],
}
MACRO_TRANSLATION_MACHINE_CONTRACT = {"texts": {"unchanged.path": "translated prose"}}


def machine_contract(task: str) -> str:
    contract = {
        "strategy_market": STRATEGY_MACHINE_CONTRACT,
        "strategy_positions": STRATEGY_MACHINE_CONTRACT,
        "macro_interpretation": MACRO_MACHINE_CONTRACT,
        "macro_translation": MACRO_TRANSLATION_MACHINE_CONTRACT,
    }.get(task)
    return canonical_json(contract) if contract is not None else ""
