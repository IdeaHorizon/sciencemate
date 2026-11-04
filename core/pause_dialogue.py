"""Discuss a pending question using the existing scheduler and pause driver."""

from __future__ import annotations

from core.decision_offer import Answer, Offer, OfferContractError, resolve_answer


def exact_pause_answer(pause, text: str):
    """Keep literal choices fast; other text belongs to the conversation."""
    try:
        from shared.lib.dangerous_commands import _BARE_INDEX

        offer = Offer.from_pause_payload(pause.to_dict())
        literal = text.strip().casefold()
        if _BARE_INDEX.fullmatch(text.strip()) or any(
            literal in {choice.id.casefold(), choice.label.strip().casefold()}
            for choice in offer.choices
        ):
            answer = resolve_answer(offer, text)
            if isinstance(answer, Answer):
                return {
                    "offer_id": answer.offer_id,
                    "choice_id": answer.choice_id,
                    "note": answer.note,
                }
    except OfferContractError:
        pass
    if (pause.metadata or {}).get("type") in {"highrisk_confirm", "permission", "capability_grant"}:
        from core.capability_grants import _NO, _YES
        from shared.lib.dangerous_commands import _APPROVE_WORDS

        if text.strip().casefold() in _YES | _NO or _APPROVE_WORDS.fullmatch(text.strip()):
            return text.strip()
    return None


def answer_from_dialogue(state, *, child_run_id: str, offer_id: str, choice_id: str, content: str):
    """Stage a choice for the driver; this tool never resumes a loop itself."""
    dialogue = state.hook_state.get("_pause_dialogue")
    if not dialogue:
        return {"status": "error", "error": "当前没有正在讨论的待答问题。"}
    pause = dialogue["pause"]
    if child_run_id != pause.asking_run_id or offer_id != (pause.offer_id or ""):
        return {"status": "error", "error": "问题已变更，请使用当前问题的 run_id 和 offer_id。"}
    if (pause.metadata or {}).get("type") in {"highrisk_confirm", "permission", "capability_grant"}:
        selection = exact_pause_answer(pause, dialogue["text"])
        if selection is None:
            return {"status": "error", "error": "讨论不代表授权；尚未收到明确的批准或拒绝。"}
    elif choice_id:
        if choice_id not in pause.choice_ids():
            return {"status": "error", "error": "该选项不属于当前问题，没有执行。"}
        selection = {"offer_id": offer_id, "choice_id": choice_id, "note": dialogue["text"]}
    else:
        if (pause.metadata or {}).get("type") == "decision_package":
            return {
                "status": "error",
                "error": "研究决定需用当前选项的 choice_id 记账；可以继续讨论。",
            }
        if content.strip() != dialogue["text"].strip():
            return {"status": "error", "error": "自由文本回答必须保留用户原话。"}
        selection = content.strip()
    dialogue["answer"] = selection
    return {"status": "success", "note": "答复已交给现有暂停驱动器；尚未执行后续工作。"}


async def discuss_pause(state, harness, llm, pause, text: str):
    import chat

    dialogue = {"pause": pause, "text": text, "answer": None}
    state.hook_state["_pause_dialogue"] = dialogue
    try:
        replies = await chat._handle_interrupt(
            state,
            harness,
            llm,
            text,
            pending_pause=pause.to_dict(),
        )
        return dialogue["answer"], "\n\n".join(replies or [])
    finally:
        state.hook_state.pop("_pause_dialogue", None)
