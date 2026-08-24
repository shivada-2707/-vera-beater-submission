"""
bot_server.py — live HTTP harness for the magicpin AI Challenge
(implements the 5 endpoints defined in challenge-testing-brief.md §2)

Run locally:
    pip install fastapi uvicorn
    uvicorn bot_server:app --host 0.0.0.0 --port 8080

Self-test locally:
    export BOT_URL=http://localhost:8080
    python judge_simulator.py

Deploy (pick one — any host that gives you a public HTTPS URL works):
    Render / Railway / Fly.io: point them at this repo, start command
        `uvicorn bot_server:app --host 0.0.0.0 --port $PORT`
    ngrok (fastest for a demo): `uvicorn bot_server:app --port 8080` then
        `ngrok http 8080` and submit the printed https://*.ngrok-free.app URL.

This process cannot itself expose a public URL — see README.md for why and
for the exact 2-minute deploy steps.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from bot import compose
from conversation_handlers import ConversationState, respond as ch_respond

app = FastAPI(title="magicpin AI Challenge — Vera-beater bot")
START = time.time()

# ---- in-memory stores (fine per §7 "must persist context until test ends") ----
contexts: dict[tuple[str, str], dict] = {}          # (scope, context_id) -> {version, payload}
conversations: dict[str, ConversationState] = {}     # conversation_id -> state
conversation_meta: dict[str, dict] = {}              # conversation_id -> {merchant_id, customer_id, trigger_id}
sent_bodies: dict[str, set] = {}                     # merchant_id -> set of bodies sent (anti-repetition, cross-conv)


def _category_for(merchant_id: str) -> Optional[dict]:
    m = contexts.get(("merchant", merchant_id))
    if not m:
        return None
    slug = m["payload"].get("category_slug")
    c = contexts.get(("category", slug))
    return c["payload"] if c else None


# =========================== 2.4 /v1/healthz ===========================

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


# =========================== 2.5 /v1/metadata ===========================

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera-beater",
        "team_members": ["magicpin AI Challenge participant"],
        "model": "template-composer (no external LLM required); optional Claude polish via ANTHROPIC_API_KEY",
        "approach": (
            "Deterministic rules/templates engine keyed on trigger.kind, reasoning over the 4 "
            "context objects exactly as specified. Falls back to real merchant signals (never "
            "fabricated data) whenever a trigger's payload is a placeholder. Optional LLM polish "
            "pass tightens phrasing without changing facts."
        ),
        "contact_email": "team@example.com",
        "version": "1.0.0",
        "submitted_at": datetime.utcnow().isoformat() + "Z",
    }


# =========================== 2.1 /v1/context ===========================

class CtxBody(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z",
    }


# =========================== 2.2 /v1/tick ===========================

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        trg_entry = contexts.get(("trigger", trg_id))
        if not trg_entry:
            continue
        trg = trg_entry["payload"]
        merchant_id = trg.get("merchant_id")
        m_entry = contexts.get(("merchant", merchant_id))
        if not m_entry:
            continue
        merchant = m_entry["payload"]
        category = _category_for(merchant_id)
        if not category:
            continue
        customer = None
        if trg.get("customer_id"):
            c_entry = contexts.get(("customer", trg["customer_id"]))
            customer = c_entry["payload"] if c_entry else None

        result = compose(category, merchant, trg, customer)

        # cross-tick anti-repetition (harness-level, on top of bot.py's own dedupe)
        seen = sent_bodies.setdefault(merchant_id, set())
        if result["body"] in seen:
            continue  # skip sending an exact repeat this tick
        seen.add(result["body"])

        conversation_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:6]}"
        state = ConversationState(conversation_id=conversation_id, merchant_id=merchant_id,
                                   customer_id=trg.get("customer_id"))
        state.bot_sent.append(result["body"])
        conversations[conversation_id] = state
        conversation_meta[conversation_id] = {"merchant_id": merchant_id, "customer_id": trg.get("customer_id"),
                                               "trigger_id": trg_id}

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": trg.get("customer_id"),
            "send_as": result["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trg.get('kind','generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", ""), trg.get("kind", "")],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        })
    return {"actions": actions}


# =========================== 2.3 /v1/reply ===========================

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.get(body.conversation_id)
    if state is None:
        state = ConversationState(conversation_id=body.conversation_id,
                                   merchant_id=body.merchant_id or "unknown",
                                   customer_id=body.customer_id)
        conversations[body.conversation_id] = state

    result = ch_respond(state, body.message)
    return result


# =========================== optional teardown ===========================

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    conversation_meta.clear()
    sent_bodies.clear()
    return {"status": "wiped"}
