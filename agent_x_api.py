"""Agent X HTTP surface - the routes the console and the main agent call.

Mounted at /agent/x (see service.py). Every route returns JSON; nothing ever
echoes an API key. Design mirrors extensions.py: stateless-ish router over a
stateful engine, failures reported, never fatal.

    GET  /agent/x/status           identity + peers + mind counts + knowledge
    GET  /agent/x/info             who this agent is (peer handshake target)
    POST /agent/x/receive          a peer Agent X delivers an envelope here
    GET  /agent/x/peers            list peers/bridges/sub-agents
    POST /agent/x/peers           add/update a peer or bridge
    DELETE /agent/x/peers/{pid}   remove one
    POST /agent/x/peers/{pid}/ping  handshake with a peer Agent X
    POST /agent/x/talk            one turn with a bridge (Claude/GPT/GLM/...)
    POST /agent/x/visit           self online_visit: fetch + learn + lessons
    POST /agent/x/delegate        give a sub-agent a task
    POST /agent/x/send            message/knowledge envelope to a peer
    GET  /agent/x/inbox            received envelopes
    POST /agent/x/inbox/read      mark envelopes read
    GET  /agent/x/mind             turns | tasks | lessons
    POST /agent/x/lesson          record a lesson by hand
    POST /agent/x/self_improve    reflect on the mind, save new lessons
    GET  /agent/x/prompt           the Agent X block for the main agent
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import agent_x
from .agent_x import get_agent_x

router = APIRouter()


def _json_ok(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse(data)


def _json_err(msg: str, code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg, "code": code}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    try:
        raw = await request.body()
        data = json.loads(raw or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# --------------------------------------------------------------------------- #
# status + handshake
# --------------------------------------------------------------------------- #
@router.get("/status")
async def status():
    ax = get_agent_x()
    peers = await ax.mesh.peers()
    turns = await ax.mesh.mind(kind="turn")
    tasks = await ax.mesh.mind(kind="" or "task")
    lessons = await ax.mesh.mind(kind="lesson")
    from .agent_x import KNOWN_ENDPOINTS
    return _json_ok({
        "enabled": True,
        "identity": ax.mesh.identity.public(),
        "peers": [p for p in peers],
        "counts": {"peers": len(peers), "bridges": len([p for p in peers if p.get("role") == "bridge"]),
                   "sub_agents": len([p for p in peers if p.get("role") in ("peer", "sub")]),
                   "turns": len(turns), "tasks": len(tasks), "lessons": len(lessons)},
        "known_endpoints": KNOWN_ENDPOINTS,
    })


@router.get("/info")
async def info():
    """The peer handshake target: GET {peer}/agent/x/info."""
    ax = get_agent_x()
    peers = await ax.mesh.peers()
    return _json_ok({"protocol": agent_x.PROTOCOL, **ax.mesh.identity.public(),
                     "endpoints": ["/agent/x/info", "/agent/x/receive"],
                     "open_tasks": len(await ax.mesh.mind(kind="task")),
                     "peers": len(peers)})

# --------------------------------------------------------------------------- #
# receive - the P2P endpoint other Agent X instances POST envelopes to
# --------------------------------------------------------------------------- #
@router.post("/receive")
async def receive(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    if body.get("protocol") != agent_x.PROTOCOL:
        return _json_err("protocol mismatch - speak agentx/1.0", "bad_protocol", 400)
    try:
        ack = await ax.receive_envelope(body)
        return _json_ok(ack)
    except Exception as err:  # noqa: BLE001 - a bad envelope must not 500 the peer
        return _json_err(f"{err.__class__.__name__}: {err}", "receive_failed", 400)


# --------------------------------------------------------------------------- #
# peers
# --------------------------------------------------------------------------- #
@router.get("/peers")
async def list_peers():
    ax = get_agent_x()
    return _json_ok({"peers": await ax.mesh.peers()})


@router.post("/peers")
async def add_peer(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    role = body.get("role") if body.get("role") in ("peer", "bridge", "sub") else "peer"
    base_url = str(body.get("base_url") or agent_x._normalise_url(""))
    if role in ("bridge", "peer", "sub") and not base_url:
        return _json_err("base_url is required", "bad_request")
    if role == "bridge":
        known = agent_x.KNOWN_ENDPOINTS.get(str(body.get("endpoint_id") or ""))
        if known and not base_url:
            base_url = known["base_url"]
    peer = await ax.mesh.upsert_peer(
        pid=str(body.get("pid") or agent_x._id("p")),
        name=str(body.get("name") or "peer"),
        role=role,
        base_url=base_url,
        model=str(body.get("model") or ""),
        api_key=str(body.get("api_key") or ""),
        meta={"added_via": body.get("added_via") or "console"})
    return _json_ok({"ok": True, "peer": peer.public()})


@router.delete("/peers/{pid}")
async def remove_peer(pid: str):
    ax = get_agent_x()
    removed = await ax.mesh.remove_peer(pid)
    if not removed:
        return _json_err(f"no peer {pid}", "no_peer", 404)
    return _json_ok({"ok": True, "removed": pid})


@router.post("/peers/{pid}/ping")
async def ping_peer(pid: str):
    ax = get_agent_x()
    return _json_ok(await ax.ping_peer(pid))


# --------------------------------------------------------------------------- #
# bridge talk + online visit + delegation
# --------------------------------------------------------------------------- #
@router.post("/talk")
async def talk(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    message = str(body.get("message") or "").strip()
    if not message:
        return _json_err("message is required", "bad_request")
    pid = str(body.get("pid") or "")
    if not pid:
        bridges = [p for p in await ax.mesh.peers() if p.get("role") == "bridge"]
        if not bridges:
            return _json_err("no bridge configured - add one: POST /agent/x/peers "
                             '{"role":"bridge","base_url":".../v1","model":"...","api_key":"..."}',
                             "no_bridge", 400)
        pid = (next((p["pid"] for p in bridges if p.get("status") == "alive"), bridges[0]["pid"]))
    return _json_ok(await ax.talk_to_bridge(pid, message,
                                            system=str(body.get("system") or "")))


@router.post("/visit")
async def visit(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    url = str(body.get("url") or "").strip()
    if not url:
        return _json_err("url is required", "bad_request")
    return _json_ok(await ax.online_visit(
        url, brief=str(body.get("brief") or ""),
        use_browser=body.get("use_browser", True) is not False))


@router.post("/delegate")
async def delegate(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    pid = str(body.get("pid") or "")
    title = str(body.get("title") or "").strip()
    goal = str(body.get("goal") or "").strip()
    if not pid or not title:
        return _json_err("pid and title are required", "bad_request")
    return _json_ok(await ax.delegate(pid, title, goal,
                                      wait=body.get("wait") is True,
                                      timeout_s=int(body.get("timeout_s") or 240)))


@router.post("/send")
async def send_envelope(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    pid = str(body.get("pid") or "")
    kind = body.get("kind") if body.get("kind") in ("message", "task", "knowledge") else "message"
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {"text": str(body.get("text") or "")}
    if not pid:
        return _json_err("pid is required", "bad_request")
    return _json_ok(await ax.send_to_peer(pid, kind, payload))


# --------------------------------------------------------------------------- #
# inbox + mind
# --------------------------------------------------------------------------- #
@router.get("/inbox")
async def inbox(pid: str = "", unread_only: bool = False):
    ax = get_agent_x()
    items = await ax.mesh.inbox(pid=pid, unread_only=unread_only)
    return _json_ok({"inbox": items, "unread": len([i for i in items if i.get("status") == "unread"])})


@router.post("/inbox/read")
async def inbox_read(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    return _json_ok({"ok": True, "marked": await ax.mesh.mark_read(str(body.get("mid") or ""))})


@router.get("/mind")
async def mind(kind: str = "", pid: str = ""):
    ax = get_agent_x()
    if kind and kind not in ("turn", "task", "lesson"):
        return _json_err("kind must be turn|task|lesson", "bad_kind")
    return _json_ok({"kind": kind or "all", "items": await ax.mesh.mind(kind=kind, pid=pid)})


@router.post("/lesson")
async def add_lesson(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    text = str(body.get("text") or "").strip()
    if not text:
        return _json_err("text is required", "bad_request")
    tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    entry = await ax.mesh.lesson(text=text, source=str(body.get("source") or "console"),
                                 tags=[str(t) for t in tags][:8])
    return _json_ok({"ok": True, "lesson": entry})


@router.post("/self_improve")
async def self_improve(request: Request):
    ax = get_agent_x()
    body = await _body(request)
    return _json_ok(await ax.self_improve(topic=str(body.get("topic") or "")))


@router.get("/prompt")
async def prompt_block():
    ax = get_agent_x()
    return _json_ok({"block": await ax.prompt_block()})
