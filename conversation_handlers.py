"""
conversation_handlers.py — optional multi-turn capability (challenge-brief.md §7.4)

respond(state, merchant_message) -> dict with keys: action, body?, cta?, rationale, wait_seconds?

Implements the two open challenges called out in the brief:
  #1 auto-reply detection (same text 3x, or known canned-reply patterns)
  #2 intent-transition handling (route "yes/let's do it" straight to action)
  #5 graceful exit (hard no, or 3 unanswered nudges)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

# --------------------------------------------------------------------------
# Conversation state (kept by the caller / bot_server.py; passed in each call)
# --------------------------------------------------------------------------

@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    turn_number: int = 0
    bot_sent: list[str] = field(default_factory=list)      # bodies bot has sent, in order
    merchant_said: list[str] = field(default_factory=list)  # raw merchant/customer replies, in order
    unanswered_nudges: int = 0
    ended: bool = False


# --------------------------------------------------------------------------
# Auto-reply / canned-reply detection
# --------------------------------------------------------------------------

CANNED_PATTERNS = [
    r"thank you for (contacting|reaching out)",
    r"we (will|shall) get back to you",
    r"automated (assistant|reply|response)",
    r"hamari team tak pahuncha",
    r"team se (baat|contact) kar",
    r"business hours",
    r"currently (unavailable|closed)",
]


def is_auto_reply(state: ConversationState, message: str) -> bool:
    # Rule 1: same message verbatim 3+ times across the conversation
    if state.merchant_said.count(message) >= 2:  # this would be the 3rd occurrence
        return True
    # Rule 2: matches a known canned-reply pattern
    low = message.lower()
    return any(re.search(p, low) for p in CANNED_PATTERNS)


# --------------------------------------------------------------------------
# Intent detection
# --------------------------------------------------------------------------

POSITIVE_INTENT = [
    r"\byes\b", r"\bya\b", r"\bhaan\b", r"\bsure\b", r"\bok(ay)?\b",
    r"go ahead", r"let'?s do it", r"sounds good", r"i want to join",
    r"mujhe.*(join|chahiye)", r"send (me|it)", r"please (do|proceed)",
]
NEGATIVE_INTENT = [
    r"\bno\b", r"\bnahi\b", r"not interested", r"\bstop\b", r"unsubscribe",
    r"don'?t (contact|message|text) me", r"maybe later", r"not now",
]
WAIT_INTENT = [
    r"give me (a|some) time", r"call you back", r"later today", r"busy right now",
    r"thodi der", r"baad me",
]


def _matches_any(patterns: list[str], text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def respond(state: ConversationState, merchant_message: str) -> dict:
    state.turn_number += 1
    state.merchant_said.append(merchant_message)

    # 1) Auto-reply detection -> try once more gently, then back off
    if is_auto_reply(state, merchant_message):
        already_nudged_autoreply = any("2 minute ka kaam" in b or "quick 2-minute" in b for b in state.bot_sent)
        if already_nudged_autoreply:
            state.ended = True
            body = "No problem — I'll reach the owner/manager directly. Wishing you continued good business! 🙂"
            state.bot_sent.append(body)
            return {"action": "end", "rationale": "Second auto-reply detected after one gentle nudge; exiting gracefully per Pattern B."}
        body = "Understood — before this goes to the team, want a quick 2-minute look yourself at what's actually missing? Happy to just do it directly if simpler."
        state.bot_sent.append(body)
        return {"action": "send", "body": body, "cta": "binary",
                "rationale": "Detected likely auto-reply (canned pattern or repeated text); trying once more before backing off."}

    # 2) Hard negative / stop -> end immediately, no further nudging
    if _matches_any(NEGATIVE_INTENT, merchant_message):
        state.ended = True
        body = "Got it, no problem — I'll stop reaching out about this. All the best!"
        state.bot_sent.append(body)
        return {"action": "end", "rationale": "Merchant signaled not-interested/stop; gracefully exiting immediately, no further nudges."}

    # 3) Explicit positive intent -> route straight to action, skip re-qualification
    if _matches_any(POSITIVE_INTENT, merchant_message):
        state.unanswered_nudges = 0
        body = "Great — starting now. I'll have this ready shortly and share it here for a final check before anything goes live."
        state.bot_sent.append(body)
        return {"action": "send", "body": body, "cta": "none",
                "rationale": "Explicit positive intent detected; routing directly to action instead of re-qualifying (avoids Pattern D failure)."}

    # 4) Wants time -> back off
    if _matches_any(WAIT_INTENT, merchant_message):
        return {"action": "wait", "wait_seconds": 1800,
                "rationale": "Merchant asked for time; backing off 30 minutes before re-engaging."}

    # 5) Curveball / anything else -> acknowledge + advance once, then track nudge count
    state.unanswered_nudges += 1
    if state.unanswered_nudges >= 3:
        state.ended = True
        body = "I'll leave this here for now — happy to pick it back up whenever works for you. Take care!"
        state.bot_sent.append(body)
        return {"action": "end", "rationale": "3 unanswered/ambiguous nudges reached; exiting gracefully rather than spamming."}

    body = "Good question — let me clarify: this is a low-effort next step (I do the work, you just approve). Want me to go ahead?"
    state.bot_sent.append(body)
    return {"action": "send", "body": body, "cta": "binary",
            "rationale": "Ambiguous/curveball reply; re-anchoring on the single low-friction ask rather than repeating the pitch."}
