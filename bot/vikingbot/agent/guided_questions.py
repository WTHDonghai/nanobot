"""Guided question suggestions for ambiguous knowledge-base requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from vikingbot.agent.intent_router import IntentDecision, IntentRoute, classify_intent
from vikingbot.providers.base import LLMProvider

SearchFn = Callable[[str, int], Awaitable[str]]

UNSAFE_LABELS = {"unsafe_override", "unsafe_internal", "unsafe_secret"}
GUIDED_QUESTION_TOKEN_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class GuidedQuestion:
    """One verified question that the UI can send back as a user message."""

    id: str
    display_text: str
    canonical_question: str
    source_uris: list[str]
    confidence: str
    token: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_text": self.display_text,
            "canonical_question": self.canonical_question,
            "source_uris": list(self.source_uris),
            "confidence": self.confidence,
            "token": self.token,
        }


@dataclass(frozen=True)
class SearchEvidence:
    """Small evidence slice extracted from OpenViking search metadata."""

    uri: str
    kind: str
    match_reason: str = ""


def should_attempt_guided_questions(user_message: str, decision: IntentDecision) -> bool:
    """Whether an intercepted request is a good candidate for guided questions."""
    if decision.label in UNSAFE_LABELS:
        return False
    if decision.route == IntentRoute.AGENT:
        return False
    if decision.route == IntentRoute.META_RESPONSE and decision.confidence == "high":
        return False

    text = " ".join(str(user_message or "").split())
    if not text:
        return False
    if len(text) > 24:
        return False
    if re.search(r"[?？!！。；;]", text):
        return False

    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_words = re.findall(r"[A-Za-z0-9]+", text)
    if cjk_count >= 2 and len(text) <= 16:
        return True
    if 1 <= len(latin_words) <= 4 and len(text) <= 24:
        return True
    return decision.confidence in {"low", "medium"} and decision.label == "out_of_scope"


async def build_guided_questions(
    *,
    provider: LLMProvider,
    generation_model: str,
    classifier_model: str,
    user_message: str,
    history: list[dict[str, Any]],
    session_id: str,
    search: SearchFn,
    token_secret: str,
    max_suggestions: int = 3,
) -> list[GuidedQuestion]:
    """Generate and verify guided questions before exposing them to clients."""
    initial_search_result = await search(user_message, 5)
    initial_evidence = extract_search_evidence(initial_search_result, limit=5)
    if not initial_evidence:
        return []

    raw_candidates = await _generate_candidate_questions(
        provider=provider,
        model=generation_model,
        user_message=user_message,
        evidence=initial_evidence,
        session_id=session_id,
        max_suggestions=max_suggestions,
    )
    if not raw_candidates:
        return []

    verified: list[GuidedQuestion] = []
    seen_questions: set[str] = set()
    initial_uris = {item.uri for item in initial_evidence}
    for index, candidate in enumerate(raw_candidates):
        canonical = _clean_question(candidate.get("canonical_question") or candidate.get("question"))
        display = _clean_question(candidate.get("display_text") or canonical)
        if not canonical or not display:
            continue
        question_key = canonical.casefold()
        if question_key in seen_questions:
            continue
        seen_questions.add(question_key)

        try:
            candidate_decision = await classify_intent(
                provider=provider,
                model=classifier_model,
                user_message=canonical,
                history=history,
                session_id=f"{session_id}::guided-question-{index}",
            )
        except Exception as exc:
            logger.debug(f"[GuidedQuestions] router validation failed: {exc}")
            continue
        if candidate_decision.route != IntentRoute.AGENT or candidate_decision.label != "knowledge_query":
            continue

        validation_search_result = await search(canonical, 5)
        validation_evidence = extract_search_evidence(validation_search_result, limit=5)
        if not validation_evidence:
            continue

        candidate_uris = _normalize_uri_list(candidate.get("source_uris"))
        validation_uris = [item.uri for item in validation_evidence]
        source_uris = _dedupe_preserve_order(
            [uri for uri in candidate_uris if uri in initial_uris]
            + [uri for uri in validation_uris if uri in initial_uris]
            + validation_uris
        )[:5]
        if not source_uris:
            continue

        suggestion_id = _stable_suggestion_id(session_id, canonical, source_uris)
        verified.append(
            GuidedQuestion(
                id=suggestion_id,
                display_text=display,
                canonical_question=canonical,
                source_uris=source_uris,
                confidence=candidate_decision.confidence,
                token=create_guided_question_token(
                    session_id=session_id,
                    canonical_question=canonical,
                    source_uris=source_uris,
                    secret=token_secret,
                ),
            )
        )
        if len(verified) >= max_suggestions:
            break

    return verified


def extract_search_evidence(search_result: str, *, limit: int = 5) -> list[SearchEvidence]:
    """Extract concrete resource URIs and match reasons from formatted search output."""
    evidence: list[SearchEvidence] = []
    current: SearchEvidence | None = None
    for line in str(search_result or "").splitlines():
        match = re.match(r"\s*\d+\.\s+\[(document|image asset)\]\s+(viking://\S+)\s*$", line)
        if match:
            if len(evidence) >= limit:
                current = None
                continue
            current = SearchEvidence(
                uri=match.group(2).strip().rstrip(".,;:"),
                kind=match.group(1),
            )
            evidence.append(current)
            continue
        reason_match = re.match(r"\s*Match reason:\s*(.+?)\s*$", line)
        if reason_match and current is not None:
            evidence[-1] = SearchEvidence(
                uri=current.uri,
                kind=current.kind,
                match_reason=reason_match.group(1).strip(),
            )
            current = evidence[-1]
    return evidence[:limit]


def create_guided_question_token(
    *,
    session_id: str,
    canonical_question: str,
    source_uris: list[str],
    secret: str,
    now: float | None = None,
) -> str:
    issued_at = int(now if now is not None else time.time())
    payload = {
        "session_id": session_id,
        "canonical_question": canonical_question,
        "source_uris": _dedupe_preserve_order(source_uris),
        "exp": issued_at + GUIDED_QUESTION_TOKEN_TTL_SECONDS,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    signature = _sign(payload_b64, secret)
    return f"{payload_b64}.{signature}"


def verify_guided_question_token(
    *,
    token: str,
    session_id: str,
    canonical_question: str,
    secret: str,
    now: float | None = None,
) -> bool:
    try:
        payload_b64, signature = str(token or "").split(".", 1)
    except ValueError:
        return False
    expected = _sign(payload_b64, secret)
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode())
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if payload.get("session_id") != session_id:
        return False
    if payload.get("canonical_question") != canonical_question:
        return False
    exp = payload.get("exp")
    try:
        return int(exp) >= int(now if now is not None else time.time())
    except (TypeError, ValueError):
        return False


async def _generate_candidate_questions(
    *,
    provider: LLMProvider,
    model: str,
    user_message: str,
    evidence: list[SearchEvidence],
    session_id: str,
    max_suggestions: int,
) -> list[dict[str, Any]]:
    evidence_lines = "\n".join(
        f"- uri: {item.uri}\n  kind: {item.kind}\n  match_reason: {item.match_reason or '(not provided)'}"
        for item in evidence
    )
    response = await provider.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "You generate guided question buttons for a document knowledge-base assistant. "
                    "Return JSON only. Generate only questions that are directly supported by the "
                    "provided search evidence. Do not answer the questions."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"short_user_message: {user_message}\n\n"
                    f"search_evidence:\n{evidence_lines}\n\n"
                    "Return a JSON object with key \"questions\". Each item must have "
                    "\"display_text\", \"canonical_question\", and \"source_uris\". "
                    f"Return at most {max_suggestions} questions. Use the user's language."
                ),
            },
        ],
        tools=None,
        model=model,
        max_tokens=420,
        temperature=0,
        session_id=f"{session_id}::guided-question-generation",
    )
    return _parse_candidate_questions(response.content)


def _parse_candidate_questions(content: str | None) -> list[dict[str, Any]]:
    text = str(content or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, dict):
        questions = parsed.get("questions")
    elif isinstance(parsed, list):
        questions = parsed
    else:
        questions = None
    if not isinstance(questions, list):
        return []
    return [item for item in questions if isinstance(item, dict)]


def _clean_question(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:120]


def _normalize_uri_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe_preserve_order(str(item).strip() for item in value if str(item).strip())


def _dedupe_preserve_order(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _stable_suggestion_id(session_id: str, canonical_question: str, source_uris: list[str]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "session_id": session_id,
                "canonical_question": canonical_question,
                "source_uris": source_uris,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f"gq_{digest[:16]}"


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(str(secret or "").encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode())
