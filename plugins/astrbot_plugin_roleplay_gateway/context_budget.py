"""Pure, bounded prompt construction from explicit persona/memory snapshots.

This module does no I/O, tokenization API calls, summarization, macro expansion,
keyword matching or native-history access. The caller supplies matched worldbook
entries and owns namespace
selection and supplies only acknowledged replies from MemoryStore. Local token
counts are deliberately conservative estimates, not exact provider token counts
or a universal upper bound. A provider's smaller known window can be supplied
through ContextBudget.total_tokens without changing the stored history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Callable

from .provider_gate import Message, PromptParts, SpeakerInput, assemble_messages


class ContextBudgetError(ValueError):
    """Static code only; no prompt, stored text or arbitrary exception details."""


@dataclass(frozen=True)
class ContextBudget:
    persona_tokens: int = 4000
    example_tokens: int = 1000
    summary_tokens: int = 1200
    recent_tokens: int = 6000
    current_tokens: int = 2000
    total_tokens: int = 16000
    structural_reserve_tokens: int = 300
    recent_turns: int = 12
    message_overhead_tokens: int = 8
    reference_tokens: int = 4000
    worldbook_tokens: int = 3000

    def __post_init__(self) -> None:
        for field, value in asdict(self).items():
            if type(value) is not int or value < 0:
                raise ContextBudgetError("invalid_context_budget")
            if field in {"persona_tokens", "current_tokens", "total_tokens"} and value == 0:
                raise ContextBudgetError("invalid_context_budget")
        if self.structural_reserve_tokens >= self.total_tokens:
            raise ContextBudgetError("invalid_context_budget")


@dataclass(frozen=True)
class PreparedPrompt:
    parts: PromptParts
    report: dict[str, Any]


def estimate_tokens(text: str) -> int:
    """ASCII ceil(chars/3), BMP non-ASCII 2, supplementary code points 4.

This portable estimate usually overcounts ordinary Chinese/English prose but
cannot guarantee a bound for every provider, tokenizer or unusual input.
"""
    if not isinstance(text, str):
        raise ContextBudgetError("invalid_context_text")
    ascii_characters = sum(ord(char) < 128 for char in text)
    non_ascii = sum(2 if ord(char) <= 0xFFFF else 4 for char in text if ord(char) >= 128)
    return (ascii_characters + 2) // 3 + non_ascii


def _text(value: Any, code: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ContextBudgetError(code)
    return value


def _attribute(value: Any, name: str, code: str) -> Any:
    try:
        return getattr(value, name)
    except (AttributeError, TypeError):
        raise ContextBudgetError(code) from None


def _speaker_data(item: Any, code: str) -> dict[str, str]:
    return {name: _text(_attribute(item, name, code), code)
            for name in ("sender_id", "display_name", "message_id", "text")}


def _history_pair(turn: Any) -> tuple[Message, ...]:
    """Only acknowledged MemoryReply records become assistant content.

    Receipt authenticity is the caller/MemoryStore integration's responsibility;
    this pure builder cannot verify a platform receipt over a network.
    """
    inputs = _attribute(turn, "inputs", "invalid_history_turn")
    replies = _attribute(turn, "replies", "invalid_history_turn")
    complete = _attribute(turn, "complete", "invalid_history_turn")
    if not isinstance(inputs, tuple) or not isinstance(replies, tuple) or (not inputs and not replies) or type(complete) is not bool:
        raise ContextBudgetError("invalid_history_turn")
    speakers = [_speaker_data(item, "invalid_history_input") for item in inputs]
    if len({item["message_id"] for item in speakers}) != len(speakers):
        raise ContextBudgetError("duplicate_history_input")
    texts = []
    previous = -1
    receipt_ids = set()
    evidence_kinds = set()
    for reply in replies:
        sequence = _attribute(reply, "sequence", "invalid_history_reply")
        status = getattr(reply, "delivery_status", "SENT")
        platform_id = _attribute(reply, "message_id", "invalid_history_reply")
        if status == "SENT":
            receipt = (status, _text(platform_id, "invalid_history_reply"))
        elif status == "API_ACCEPTED" and platform_id is None:
            receipt = (status, _text(_attribute(reply, "delivery_ref", "invalid_history_reply"), "invalid_history_reply"))
        else:
            raise ContextBudgetError("invalid_history_reply_evidence")
        if type(sequence) is not int or sequence < 0 or sequence <= previous or receipt in receipt_ids:
            raise ContextBudgetError("invalid_history_reply_order")
        previous = sequence
        receipt_ids.add(receipt)
        evidence_kinds.add(status)
        texts.append(_text(_attribute(reply, "text", "invalid_history_reply"), "invalid_history_reply"))
    delivery = {"complete": complete, "confirmed_segments": len(texts)}
    if "API_ACCEPTED" in evidence_kinds:
        delivery["evidence"] = sorted(evidence_kinds)
    user = json.dumps({"current_speaker_batch": speakers,
                       "reply_delivery": delivery}, ensure_ascii=False)
    # QQ bubbles are joined with an explicit paragraph separator. No generated
    # draft, hidden tail, tool role or stored input-provided role is consulted.
    return ((Message("user", user),) if speakers else ()) + ((Message("assistant", "\n\n".join(texts)),) if texts else ())


def _example_pairs(examples: Any) -> list[tuple[Message, Message]]:
    if not isinstance(examples, tuple) or len(examples) % 2:
        raise ContextBudgetError("invalid_examples")
    pairs = []
    for index in range(0, len(examples), 2):
        user, assistant = examples[index:index + 2]
        if (not isinstance(user, Message) or not isinstance(assistant, Message)
                or user.role != "user" or assistant.role != "assistant"):
            raise ContextBudgetError("invalid_examples")
        _text(user.content, "invalid_examples")
        _text(assistant.content, "invalid_examples")
        pairs.append((user, assistant))
    return pairs


def build_prompt(persona: Any, memory: Any, current: tuple[SpeakerInput, ...], *,
                 runtime_boundary: str, budget: ContextBudget | None = None,
                 assistant_projection: Callable[[str], str] | None = None,
                 reference_data: str = "", before_worldbook: tuple[str, ...] = (),
                 after_worldbook: tuple[str, ...] = ()) -> PreparedPrompt:
    """Preserve fixed content; drop oldest whole history turns before examples.

    persona and memory follow the PersonaSnapshot / MemorySnapshot contracts.
    Structural duck typing keeps this pure layer independent of disk loaders.
    Both source snapshots are left unchanged. An optional assistant projection
    is applied before budgeting; an empty result drops the whole dialogue pair.
    It never changes current/user input or the stored memory. An overlarge persona, current
    input, summary, or remaining fixed context fails before any model request.
    """
    limits = budget if budget is not None else ContextBudget()
    if not isinstance(limits, ContextBudget):
        raise ContextBudgetError("invalid_context_budget")
    boundary = _text(runtime_boundary, "invalid_runtime_boundary")
    if not isinstance(before_worldbook, tuple) or not isinstance(after_worldbook, tuple):
        raise ContextBudgetError('invalid_worldbook')
    world_cost = sum(estimate_tokens(_text(t, 'invalid_worldbook')) + limits.message_overhead_tokens
                     for t in before_worldbook + after_worldbook)
    if world_cost > limits.worldbook_tokens:
        raise ContextBudgetError('worldbook_budget_exceeded')
    reference_data = _text(reference_data, "invalid_reference_data", empty=True)
    reference_cost = (estimate_tokens(json.dumps({"untrusted_reference_data": reference_data},
                                                 ensure_ascii=False)) + limits.message_overhead_tokens
                      if reference_data else 0)
    if reference_cost > limits.reference_tokens:
        raise ContextBudgetError("reference_budget_exceeded")
    persona_id = _text(_attribute(persona, "persona_id", "invalid_persona"), "invalid_persona")
    prompt = _text(_attribute(persona, "prompt", "invalid_persona"), "invalid_persona")
    namespace = _attribute(memory, "namespace", "invalid_memory_snapshot")
    if _attribute(namespace, "persona_id", "invalid_memory_namespace") != persona_id:
        raise ContextBudgetError("memory_persona_mismatch")
    summary = _text(_attribute(memory, "summary", "invalid_memory_snapshot"), "invalid_summary", empty=True)
    turns = _attribute(memory, "turns", "invalid_memory_snapshot")
    if not isinstance(turns, tuple):
        raise ContextBudgetError("invalid_history_collection")
    if not isinstance(current, tuple) or not current or any(not isinstance(item, SpeakerInput) for item in current):
        raise ContextBudgetError("invalid_current_input")
    current_rows = [_speaker_data(item, "invalid_current_input") for item in current]
    if len({item["message_id"] for item in current_rows}) != len(current_rows):
        raise ContextBudgetError("duplicate_current_input")

    base = PromptParts(runtime_boundary=boundary, persona=prompt, summary=summary, current=current)
    # Reuse the actual request assembler for the current JSON envelope.
    current_text = assemble_messages(base)[-1].content
    persona_cost = estimate_tokens(prompt)
    summary_cost = estimate_tokens(summary)
    current_cost = estimate_tokens(current_text) + limits.message_overhead_tokens
    if persona_cost > limits.persona_tokens:
        raise ContextBudgetError("persona_budget_exceeded")
    if current_cost > limits.current_tokens:
        raise ContextBudgetError("current_input_budget_exceeded")
    if summary_cost > limits.summary_tokens:
        raise ContextBudgetError("summary_budget_exceeded")

    def pair_cost(pair: tuple[Message, ...]) -> int:
        return sum(estimate_tokens(message.content) + limits.message_overhead_tokens for message in pair)

    def project_pair(pair: tuple[Message, ...]) -> tuple[Message, ...] | None:
        if assistant_projection is None:
            return pair
        if pair[-1].role != 'assistant':
            return pair
        content = assistant_projection(pair[-1].content)
        if not isinstance(content, str):
            raise ContextBudgetError("invalid_assistant_projection")
        return (*pair[:-1], Message("assistant", content)) if content.strip() else None

    examples = _example_pairs(_attribute(persona, "examples", "invalid_persona"))
    example_count = len(examples)
    examples = [projected for pair in examples if (projected := project_pair(pair)) is not None]
    example_costs = [pair_cost(pair) for pair in examples]
    examples_cost = sum(example_costs)
    while examples and examples_cost > limits.example_tokens:
        examples.pop()
        examples_cost -= example_costs.pop()

    selected = turns[-limits.recent_turns:] if limits.recent_turns else ()
    previous_sequence = -1
    history = []
    for turn in selected:
        sequence = _attribute(turn, "sequence", "invalid_history_turn")
        if type(sequence) is not int or sequence < 0 or sequence <= previous_sequence:
            raise ContextBudgetError("invalid_history_turn_order")
        previous_sequence = sequence
        pair = project_pair(_history_pair(turn))
        if pair is not None:
            history.append(pair)
    history_costs = [pair_cost(pair) for pair in history]
    recent_cost = sum(history_costs)
    while history and recent_cost > limits.recent_tokens:
        history.pop(0)
        recent_cost -= history_costs.pop(0)

    # Per-message framing is counted in addition to the declared global reserve.
    fixed = (estimate_tokens(boundary) + persona_cost + summary_cost + current_cost + reference_cost + world_cost
             + limits.message_overhead_tokens * (2 + bool(summary))
             + limits.structural_reserve_tokens)
    total = fixed + recent_cost + examples_cost
    while history and total > limits.total_tokens:
        history.pop(0)
        dropped_cost = history_costs.pop(0)
        recent_cost -= dropped_cost
        total -= dropped_cost
    while examples and total > limits.total_tokens:
        examples.pop()
        dropped_cost = example_costs.pop()
        examples_cost -= dropped_cost
        total -= dropped_cost
    if total > limits.total_tokens:
        raise ContextBudgetError("fixed_context_budget_exceeded")

    parts = PromptParts(runtime_boundary=boundary, persona=prompt, summary=summary,
                        before_worldbook=before_worldbook, after_worldbook=after_worldbook,
                        current=current, reference_data=reference_data,
                        examples=tuple(message for pair in examples for message in pair),
                        recent=tuple(message for pair in history for message in pair))
    report = {
        "token_count_method": "ascii_ceil_chars_div3_bmp_nonascii_x2_supplementary_x4_plus_framing",
        "exact_provider_token_count": False,
        "guaranteed_token_upper_bound": False,
        "limits": asdict(limits),
        "estimated_tokens": {"runtime_boundary": estimate_tokens(boundary),
                             "persona": persona_cost, "summary": summary_cost,
                             "reference": reference_cost,
                             "worldbook": world_cost,
                             "examples": examples_cost, "recent": recent_cost,
                             "current_with_envelope": current_cost,
                             "system_message_overhead": limits.message_overhead_tokens * (2 + bool(summary)),
                             "structural_reserve": limits.structural_reserve_tokens,
                             "total": total},
        "history_turns_available": len(turns),
        "history_turns_included": len(history),
        "history_turns_dropped": len(turns) - len(history),
        "example_pairs_available": example_count,
        "example_pairs_included": len(examples),
        "example_pairs_dropped": example_count - len(examples),
        "current_speakers": len(current),
        "unsupported_features": ["macro_expansion"],
    }
    return PreparedPrompt(parts, report)
