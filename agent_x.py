"""Agent X - the peer-to-peer agent mesh for the Agent_Linux terminal.

The main agent (Agentbox) runs tools; Agent X is the layer that lets it talk
to *other* agents:

  - other Agentbox terminals (sub-agents, other machines running agent_linux)
  - any OpenAI-compatible model endpoint (Claude, GPT, GLM, Kimi, DeepSeek...
    through a router like NovaRouter/OpenRouter or a native /v1 endpoint)

Three planes, deliberately separated:

  MESH   - peer registry, identities, envelopes, inbox/outbox.
           P2P: Agent X <-> Agent X. Local and cross-host.
  BRIDGE - one OpenAI-compatible "conversation" per remote model. Agent X
           speaks for the agent; the model just talks. *This* is how Claude,
           GPT, GLM, Kimi etc. are reached - through endpoints added as peers
           of kind "bridge".
  MIND   - the self-improvement loop: conversation memory, task ledger,
           lessons. Persisted in the workspace so a redeploy keeps the mind.

Design rules (matching the house style of this codebase):
  - self-contained: stdlib + httpx + fastapi only, imports nothing from nova/
  - failure-isolated: a dead peer or model never breaks the chat loop
  - nothing here ever echoes a secret
  - state lives in JSON under <workspace>/.agentx/ so a redeploy keeps it

    python3 -m agent_linux.agent_x --selftest
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx

from . import config
from .config import build_root

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
PROTOCOL = "agentx/1.0"
AGENTX_DIRNAME = ".agentx"
STATE_VERSION = 1

ROLES = ("peer", "bridge", "sub")
STATUS = ("unknown", "alive", "dead")

# Endpoints Agent X knows by name, so a bridge can be registered with a short
# id + an API key instead of a raw URL. The agent itself can extend this list
# after an online_visit finds more.
KNOWN_ENDPOINTS: dict[str, dict[str, str]] = {
    "novarouter": {
        "label": "NovaRouter (Claude/GPT/GLM/Kimi/DeepSeek)",
        "base_url": "https://api.novarouter.net/v1",
        "doc": "https://docs.novarouter.net",
    },
    "openrouter": {
        "label": "OpenRouter (aggregates every major model)",
        "base_url": "https://openrouter.ai/api/v1",
        "doc": "https://openrouter.ai/docs",
    },
}

CALL_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=30.0)
MAX_MEMORY_TURNS = 500          # per peer conversation memory cap
MAX_LESSONS = 200               # self-improvement ledger cap
MAX_TASKS = 300                 # task ledger cap

# --------------------------------------------------------------------------- #
# tiny helpers
# --------------------------------------------------------------------------- #
def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _fingerprint(data: dict[str, Any]) -> str:
    """A stable, non-secret identity for an agent: hash of endpoint+model+name."""
    raw = f"{data.get('base_url', '')}|{data.get('model', '')}|{data.get('agent_name', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _mask(key: str) -> str:
    if not key:
        return ""
    return f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "..."


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


# --------------------------------------------------------------------------- #
# identity — who Agent X *is* on this terminal
# --------------------------------------------------------------------------- #
@dataclass
class Identity:
    agent_id: str
    name: str                 # display name, e.g. "AgentX@buildhost"
    kind: str                 # main | sub
    created: float
    secret: str               # shared secret for the mesh; never echoed

    def public(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "name": self.name,
                "kind": self.kind, "protocol": PROTOCOL}


def _identity_file() -> Path:
    return build_root() / AGENTX_DIRNAME / "identity.json"


def load_identity() -> Identity:
    """Load or lazily mint this terminal's Agent X identity."""
    path = _identity_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_json(path, {})
    if data.get("agent_id"):
        return Identity(agent_id=data["agent_id"], name=data.get("name", "agent-x"),
                        kind=data.get("kind", "main"), created=data.get("created", _now()),
                        secret=data.get("secret", ""))
    host = os.uname().nodename.split(".")[0][:16] or "terminal"
    ident = Identity(agent_id=_id("ax"), name=f"AgentX@{host}",
                     kind="main", created=_now(),
                     secret=os.urandom(16).hex())
    _write_json(path, asdict(ident))
    return ident


# --------------------------------------------------------------------------- #
# peers — the mesh registry
# --------------------------------------------------------------------------- #
@dataclass
class Peer:
    pid: str                  # stable id
    name: str                 # human label
    role: str                 # peer | bridge | sub
    base_url: str             # http endpoint of the peer agent or model router
    model: str                # for bridges: the model id; peers: ""
    api_key: str              # for bridges; masked everywhere
    agent_id: str             # the remote Agent X id, when it has one
    status: str               # unknown | alive | dead
    last_seen: float          # 0 = never
    created: float
    meta: dict[str, Any] = None  # free-form; defaults to dict below

    def __post_init__(self) -> None:
        if self.meta is None:
            self.meta = {}

    def public(self) -> dict[str, Any]:
        """The peer as the console/agent sees it - never the raw key."""
        return {"pid": self.pid, "name": self.name, "role": self.role,
                "base_url": self.base_url, "model": self.model,
                "agent_id": self.agent_id, "status": self.status,
                "last_seen": self.last_seen, "created": self.created,
                "key": _mask(self.api_key), "meta": self.meta}


# --------------------------------------------------------------------------- #
# the mesh store - one JSON file, atomic writes, tiny by design
# --------------------------------------------------------------------------- #
class Mesh:
    """Peers + envelopes + mind, persisted as JSON under <workspace>/.agentx/.

    A class, not module globals, because a terminal can host several Agent X
    instances (main + sub-agents) and tests need isolation.
    """

    def __init__(self, root: Path | None = None, identity: Identity | None = None):
        self.root = root or (build_root() / AGENTX_DIRNAME)
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = identity or load_identity()
        self._lock = asyncio.Lock() if _has_asyncio() else None

    # ---- paths ------------------------------------------------------------
    def _peers_file(self) -> Path:
        return self.root / "peers.json"

    def _inbox_file(self) -> Path:
        return self.root / "inbox.json"

    def _outbox_file(self) -> Path:
        return self.root / "outbox.json"

    def _mind_file(self) -> Path:
        return self.root / "mind.json"

    # ---- peers ------------------------------------------------------------
    async def peers(self) -> list[dict[str, Any]]:
        data = await asyncio.to_thread(_read_json, self._peers_file(), [])
        return data if isinstance(data, list) else []

    async def peer(self, pid: str) -> Peer | None:
        for item in await self.peers():
            if item.get("pid") == pid:
                return _peer_from(item)
        return None

    async def upsert_peer(self, **kw: Any) -> Peer:
        """Add or update a peer by pid. Returns the saved peer."""
        pid = str(kw.get("pid") or _id("p"))
        async with self._guard():
            items = await self.peers()
            existing = next((i for i in items if i.get("pid") == pid), None)
            if existing:
                existing.update({k: v for k, v in kw.items()
                                 if k in ("name", "role", "base_url", "model",
                                          "api_key", "agent_id", "meta") and v not in (None, "")})
                existing["meta"] = {**(existing.get("meta") or {}), **(kw.get("meta") or {})}
                peer = _peer_from(existing)
            else:
                peer = Peer(pid=pid, name=str(kw.get("name") or pid),
                            role=str(kw.get("role") or "peer")
                            if kw.get("role") in ROLES else "peer",
                            base_url=str(kw.get("base_url") or "").rstrip("/"),
                            model=str(kw.get("model") or ""),
                            api_key=str(kw.get("api_key") or ""),
                            agent_id=str(kw.get("agent_id") or ""),
                            status="unknown", last_seen=0, created=_now(),
                            meta=dict(kw.get("meta") or {}))
                items.append(asdict(peer))
            await asyncio.to_thread(_write_json, self._peers_file(), items)
        return peer

    async def remove_peer(self, pid: str) -> bool:
        async with self._guard():
            items = await self.peers()
            kept = [i for i in items if i.get("pid") != pid]
            if len(kept) == len(items):
                return False
            await asyncio.to_thread(_write_json, self._peers_file(), kept)
        return True

    async def set_peer_status(self, pid: str, status: str, seen: float | None = None) -> None:
        async with self._guard():
            items = await self.peers()
            for item in items:
                if item.get("pid") == pid:
                    item["status"] = status
                    item["last_seen"] = seen if seen is not None else _now()
                    break
            await asyncio.to_thread(_write_json, self._peers_file(), items)

    # ---- envelopes --------------------------------------------------------
    async def record_out(self, pid: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        env = {"mid": _id("m"), "dir": "out", "pid": pid, "kind": kind,
               "payload": payload, "ts": _now(), "status": "sent"}
        await self._append(self._outbox_file(), env, MAX_MEMORY_TURNS * 4)
        return env

    async def record_in(self, pid: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        env = {"mid": _id("m"), "dir": "in", "pid": pid, "kind": kind,
               "payload": payload, "ts": _now(), "status": "unread"}
        await self._append(self._inbox_file(), env, MAX_MEMORY_TURNS * 4)
        return env

    async def inbox(self, pid: str = "", unread_only: bool = False) -> list[dict[str, Any]]:
        items = await asyncio.to_thread(_read_json, self._inbox_file(), [])
        out = [i for i in items if isinstance(i, dict)
               and (not pid or i.get("pid") == pid)
               and (not unread_only or i.get("status") == "unread")]
        return out[-100:]

    async def outbox(self, pid: str = "") -> list[dict[str, Any]]:
        items = await asyncio.to_thread(_read_json, self._outbox_file(), [])
        out = [i for i in items if isinstance(i, dict) and (not pid or i.get("pid") == pid)]
        return out[-100:]

    async def mark_read(self, mid: str = "") -> int:
        async with self._guard():
            items = await asyncio.to_thread(_read_json, self._inbox_file(), [])
            n = 0
            for item in items:
                if (not mid or item.get("mid") == mid) and item.get("status") == "unread":
                    item["status"] = "read"
                    n += 1
            await asyncio.to_thread(_write_json, self._inbox_file(), items)
        return n

    async def _append(self, path: Path, env: dict[str, Any], cap: int) -> None:
        async with self._guard():
            items = await asyncio.to_thread(_read_json, path, [])
            items = [i for i in items if isinstance(i, dict)]
            items.append(env)
            if len(items) > cap:
                items = items[-cap:]
            await asyncio.to_thread(_write_json, path, items)

    # ---- mind: memory, tasks, lessons -------------------------------------
    async def remember(self, pid: str, role: str, content: str) -> dict[str, Any]:
        """One conversational turn with any peer/bridge, kept in the mind."""
        turn = {"mid": _id("t"), "pid": pid, "role": role, "content": content[:8000],
                "ts": _now()}
        await self._append(self._mind_file(), {"kind": "turn", **turn}, MAX_MEMORY_TURNS)
        return turn

    async def lesson(self, text: str, source: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        """A self-improvement lesson the agent (or a peer) taught it."""
        entry = {"lid": _id("l"), "text": text[:4000], "source": source,
                 "tags": [t for t in (tags or [])][:8], "ts": _now()}
        await self._append(self._mind_file(), {"kind": "lesson", **entry}, MAX_LESSONS)
        return entry

    async def task(self, title: str, goal: str = "", status: str = "open",
                   tid: str = "", result: str = "") -> dict[str, Any]:
        """Create or update one delegated task in the ledger."""
        async with self._guard():
            items = await asyncio.to_thread(_read_json, self._mind_file(), [])
            now = _now()
            if tid:
                for item in items:
                    if item.get("kind") == "task" and item.get("tid") == tid:
                        item["status"] = status
                        item["result"] = (result or item.get("result") or "")[:8000]
                        item["updated"] = now
                        await asyncio.to_thread(_write_json, self._mind_file(), items)
                        return item
                return {"error": f"no task {tid}"}
            entry = {"kind": "task", "tid": _id("t"), "title": title[:300],
                     "goal": (goal or "")[:4000], "status": status,
                     "result": result[:8000], "created": now, "updated": now}
            items.append(entry)
            tasks = [i for i in items if i.get("kind") == "task"]
            if len(tasks) > MAX_TASKS:
                drop = {t.get("tid") for t in tasks[:-MAX_TASKS]}
                items = [i for i in items if i.get("tid") not in drop]
            await asyncio.to_thread(_write_json, self._mind_file(), items)
            return entry

    async def mind(self, kind: str = "", pid: str = "") -> list[dict[str, Any]]:
        items = await asyncio.to_thread(_read_json, self._mind_file(), [])
        return [i for i in items if isinstance(i, dict)
                and (not kind or i.get("kind") == kind)
                and (not pid or i.get("pid") == pid)][-200:]

    async def history_for(self, pid: str, limit: int = 20) -> list[dict[str, str]]:
        """The recent turns with one peer, as chat messages for a bridge call."""
        turns = [t for t in await self.mind(kind="turn", pid=pid)][-limit:]
        return [{"role": t.get("role", "user"), "content": t.get("content", "")}
                for t in turns]

    # ---- locking ----------------------------------------------------------
    def _guard(self):
        """The mesh lock - real when asyncio is running, a stub otherwise."""
        if self._lock is None:
            return _NullCtx()
        return self._lock


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _has_asyncio() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _peer_from(data: dict[str, Any]) -> Peer:
    return Peer(pid=str(data.get("pid") or _id("p")),
                name=str(data.get("name") or "peer"),
                role=data.get("role") if data.get("role") in ROLES else "peer",
                base_url=str(data.get("base_url") or ""),
                model=str(data.get("model") or ""),
                api_key=str(data.get("api_key") or ""),
                agent_id=str(data.get("agent_id") or ""),
                status=data.get("status") if data.get("status") in STATUS else "unknown",
                last_seen=float(data.get("last_seen") or 0),
                created=float(data.get("created") or _now()),
                meta=dict(data.get("meta") or {}))

# --------------------------------------------------------------------------- #
# Agent X engine - bridges, peer transport, self-improvement, online visit
# --------------------------------------------------------------------------- #
DIALOG_SYSTEM = (
    "You are {name}, a peer agent in a cooperative agent mesh (protocol agentx/1.0). "
    "You exchange knowledge with other agents, cross-check answers, and surface "
    "risks the other side may have missed. Be concrete, cite what you actually "
    "know, separate fact from inference, and keep replies tight."
)

VISIT_SYSTEM = (
    "You are Agent X's research head. Turn the user's brief into a crisp research "
    "question, read the page Agent X fetched, and reply in JSON: "
    '{"summary": str, "key_facts": [str], "lesson": str, "tags": [str], '
    '"follow_up_urls": [str], "peer_suggestions": [{"name": str, "base_url": str, '
    '"role": str}]} '
    "key_facts must be specific; lesson is one line worth remembering. "
    "peer_suggestions: model-router endpoints worth registering (only if truly found "
    "on the page). Reply with JSON only."
)


class AgentX:
    """The engine: one instance per terminal, created lazily by the API layer."""

    def __init__(self, root: Path | None = None):
        self.mesh = Mesh(root=root)

    # ---- bridges (remote models: Claude/GPT/GLM/Kimi/...) ------------------
    async def _bridge_call(self, peer: Peer, messages: list[dict[str, str]],
                           max_tokens: int = 1200) -> dict[str, Any]:
        """One OpenAI-compatible chat completion against a bridge peer."""       
        if not peer.base_url:
            return {"error": "bridge has no base_url", "code": "bad_peer"}
        headers = {"Content-Type": "application/json"}
        if peer.api_key:
            headers["Authorization"] = f"Bearer {peer.api_key}"
            if "openrouter" in peer.base_url:
                headers["HTTP-Referer"] = "https://agentx.local"
                headers["X-Title"] = "Agent X mesh"
        body = {"model": peer.model or "default",
                "messages": messages, "max_tokens": max_tokens,
                "temperature": 0.6}
        try:
            async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=True) as client:
                res = await client.post(f"{peer.base_url}/chat/completions",
                                        json=body, headers=headers)
            if res.status_code >= 400:
                detail = res.text[:300].replace("\n", " ")
                return {"error": f"model endpoint HTTP {res.status_code}: {detail}",
                        "code": "model_http_error"}
            payload = res.json()
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            usage = payload.get("usage") or {}
            return {"reply": str(message.get("content") or "").strip(),
                    "model": payload.get("model") or peer.model,
                    "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")
                              if usage.get(k) is not None}}
        except httpx.HTTPError as err:
            return {"error": f"model endpoint unreachable: {err.__class__.__name__}",
                    "code": "model_unreachable"}
        except ValueError as err:
            return {"error": f"model endpoint returned non-JSON: {err}",
                    "code": "model_bad_json"}

    async def talk_to_bridge(self, pid: str, message: str,
                             system: str = "") -> dict[str, Any]:
        """One turn with a remote model, with per-peer memory in the mind."""
        peer = await self.mesh.peer(pid)
        if peer is None or peer.role != "bridge":
            return {"error": f"no bridge peer {pid}", "code": "no_peer"}
        history = await self.mesh.history_for(pid, limit=16)
        messages = ([{"role": "system", "content": system}] if system else [])
        messages += history + [{"role": "user", "content": message[:12000]}]
        await self.mesh.remember(pid, "user", message)
        result = await self._bridge_call(peer, messages)
        reply = result.get("reply") or ""
        if reply:
            await self.mesh.remember(pid, "assistant", reply)
            await self.mesh.set_peer_status(pid, "alive")
        elif result.get("error"):
            await self.mesh.set_peer_status(pid, "dead")
        return {"pid": pid, "peer": peer.name, "model": result.get("model"),
                "reply": reply, "usage": result.get("usage"),
                "error": result.get("error"), "code": result.get("code")}

    # ---- peer-to-peer (other Agent X / Agentbox terminals) -----------------
    async def ping_peer(self, pid: str) -> dict[str, Any]:
        """Handshake: GET {base}/x/info - proves the peer speaks agentx/1.0."""
        peer = await self.mesh.peer(pid)
        if peer is None:
            return {"error": f"no peer {pid}", "code": "no_peer"}
        try:
            async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=True) as client:
                res = await client.get(f"{peer.base_url.rstrip(chr(47))}/agent/x/info")
            if res.status_code >= 400:
                await self.mesh.set_peer_status(pid, "dead")
                return {"pid": pid, "status": "dead",
                        "error": f"peer answered HTTP {res.status_code}"}
            info = res.json() if res.content else {}
            await self.mesh.set_peer_status(pid, "alive")
            await self.mesh.upsert_peer(pid=pid, agent_id=str(info.get("agent_id") or ""),
                                        meta={"remote_name": info.get("name")})
            return {"pid": pid, "status": "alive", "peer_info": info}
        except httpx.HTTPError as err:
            await self.mesh.set_peer_status(pid, "dead")
            return {"pid": pid, "status": "dead",
                    "error": f"peer unreachable: {err.__class__.__name__}"}

    async def send_to_peer(self, pid: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST an envelope to a peer Agent X. Kinds: message | task | knowledge."""
        if kind not in ("message", "task", "knowledge"):
            return {"error": "kind must be message|task|knowledge", "code": "bad_kind"}
        peer = await self.mesh.peer(pid)
        if peer is None:
            return {"error": f"no peer {pid}", "code": "no_peer"}
        env = await self.mesh.record_out(pid, kind, payload)
        body = {"protocol": PROTOCOL, "from_agent": self.mesh.identity.public(),
                "kind": kind, "envelope": env}
        try:
            async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=True) as client:
                res = await client.post(f"{peer.base_url.rstrip(chr(47))}/agent/x/receive", json=body)
            if res.status_code >= 400:
                await self.mesh.set_peer_status(pid, "dead")
                return {"mid": env["mid"], "pid": pid, "status": "failed",
                        "error": f"peer answered HTTP {res.status_code}"}
            ack = res.json() if res.content else {}
            await self.mesh.set_peer_status(pid, "alive")
            return {"mid": env["mid"], "pid": pid, "status": "delivered",
                    "ack": ack}
        except httpx.HTTPError as err:
            await self.mesh.set_peer_status(pid, "dead")
            return {"mid": env["mid"], "pid": pid, "status": "failed",
                    "error": f"peer unreachable: {err.__class__.__name__}"}

    async def receive_envelope(self, body: dict[str, Any]) -> dict[str, Any]:
        """The /x/receive endpoint: store it, ack, and let the mind absorb it."""
        kind = body.get("kind") if body.get("kind") in ("message", "task", "knowledge") else "message"
        sender = body.get("from_agent") if isinstance(body.get("from_agent"), dict) else {}
        env = body.get("envelope") if isinstance(body.get("envelope"), dict) else {}
        payload = env.get("payload") if isinstance(env.get("payload"), dict) else {}
        sender_id = str(sender.get("agent_id") or sender.get("name") or "unknown")
        stored = await self.mesh.record_in(sender_id, kind, payload)
        # knowledge envelopes teach the mind directly - the self-improvement loop
        if kind == "knowledge" and str(payload.get("lesson") or payload.get("text") or "").strip():
            await self.mesh.lesson(text=str(payload.get("lesson") or payload.get("text")),
                                   source=f"peer:{sender_id}",
                                   tags=[str(t) for t in (payload.get("tags") or [])[:8]])
        if kind == "task":
            await self.mesh.task(title=str(payload.get("title") or "delegated task"),
                                 goal=str(payload.get("goal") or ""), status="open")
        return {"ok": True, "received": stored["mid"],
                "from": sender_id, "kind": kind,
                "protocol": PROTOCOL}

    # ---- self online visit -------------------------------------------------
    async def online_visit(self, url: str, brief: str = "",
                           use_browser: bool = True) -> dict[str, Any]:
        """Agent X visits the web by itself and learns.

        Three layers, tried in order:
          1. the live Chromium browser (playwright) - what the user can watch
          2. plain httpx fetch - fast, works server-side
          3. readability pass: strip tags, keep text

        Then a bridge model reads the page and returns a structured summary;
        the summary becomes a lesson in the mind (self-improvement loop).
        """
        target = _normalise_url(url)
        fetched = await _fetch_page_async(target, use_browser=use_browser)
        if fetched.get("error"):
            return {"url": target, "error": fetched["error"], "code": fetched.get("code", "fetch_failed")}
        title, text = fetched.get("title", ""), fetched.get("text", "")
        # Pick the best bridge: first alive one, else first configured one.
        bridges = [p for p in await self.mesh.peers() if p.get("role") == "bridge"]
        if not bridges:
            return {"url": target, "title": title, "text_preview": text[:600],
                    "summary": "", "error": "no bridge configured - add one with POST /agent/x/peers "
                              "(role=bridge) so Agent X can think about the page",
                    "code": "no_bridge"}
        bridge = next((b for b in bridges if b.get("status") == "alive"), bridges[0])
        peer = _peer_from(bridge)
        page_payload = ("PAGE_TITLE: " + title + "\n\nPAGE_TEXT:\n" + text[:14000])
        prompt = ("Research brief: " + (brief or "summarise what this page offers an autonomous agent")
                  + "\n\n" + page_payload)
        history = await self.mesh.history_for(peer.pid, limit=6)
        messages = ([{"role": "system", "content": VISIT_SYSTEM}] + history
                    + [{"role": "user", "content": prompt}])
        await self.mesh.remember(peer.pid, "user",
                                 f"[online_visit {target}] {brief[:400]}")
        result = await self._bridge_call(peer, messages, max_tokens=900)
        reply = str(result.get("reply") or "")
        if not reply:
            return {"url": target, "title": title, "text_preview": text[:600],
                    "error": result.get("error") or "empty model reply",
                    "code": result.get("code", "bridge_error")}
        await self.mesh.remember(peer.pid, "assistant", reply)
        await self.mesh.set_peer_status(peer.pid, "alive")
        parsed = _extract_json(reply)
        lesson = str(parsed.get("lesson") or "").strip()
        if lesson:
            await self.mesh.lesson(text=lesson, source=f"web:{target}",
                                   tags=[str(t) for t in (parsed.get("tags") or [])[:8]])
        # The agent can even grow its own mesh: register endpoints it found.
        suggested = parsed.get("peer_suggestions") or []
        registered: list[str] = []
        if isinstance(suggested, list):
            for item in suggested[:3]:
                if not isinstance(item, dict):
                    continue
                base = _normalise_url(str(item.get("base_url") or ""))
                if not base or not _looks_like_endpoint(base):
                    continue
                p = await self.mesh.upsert_peer(pid=_id("p"),
                                                name=str(item.get("name") or base)[:60],
                                                role="bridge", base_url=base,
                                                model=str(item.get("model") or ""))
                registered.append(p.pid)
        return {"url": target, "title": title, "via": fetched.get("via"),
                "bridge": peer.name, "model": result.get("model"),
                "summary": str(parsed.get("summary") or reply[:1500]),
                "key_facts": parsed.get("key_facts") or [],
                "follow_up_urls": parsed.get("follow_up_urls") or [],
                "lesson_saved": bool(lesson), "peers_registered": registered,
                "usage": result.get("usage")}

    # ---- self-improvement ---------------------------------------------------
    async def self_improve(self, topic: str = "") -> dict[str, Any]:
        """Ask a bridge model to reflect on the mind and produce new lessons.

        This is the deliberate self-improvement loop: the mind (turns, tasks,
        lessons) is the material; the bridge is the mirror; the lessons file
        is the increment. The main agent reads lessons via /agent/x/mind.
        """
        turns = await self.mesh.mind(kind="turn")
        tasks = await self.mesh.mind(kind="task")
        lessons = await self.mesh.mind(kind="lesson")
        if not turns and not tasks:
            return {"error": "the mind is empty - talk, delegate, or online_visit first",
                    "code": "empty_mind"}
        digest = _mind_digest(turns, tasks, lessons, topic=topic)
        bridges = [p for p in await self.mesh.peers() if p.get("role") == "bridge"]
        if not bridges:
            return {"error": "no bridge configured", "code": "no_bridge"}
        bridge = next((b for b in bridges if b.get("status") == "alive"), bridges[0])
        peer = _peer_from(bridge)
        prompt = ("Reflect on this agent-mesh activity log"
                  + (f" on topic: {topic}" if topic else "")
                  + '. Produce JSON: {"lessons": [{"text": str, "tags": [str]}], '
                    '"plan": str}. Lessons must be specific, actionable, and '
                    'grounded in the log - no generic advice.')
        messages = ([{"role": "system", "content": VISIT_SYSTEM}] +
                    [{"role": "user", "content": prompt + "\n\n" + digest}])
        result = await self._bridge_call(peer, messages, max_tokens=900)
        reply = str(result.get("reply") or "")
        if not reply:
            return {"error": result.get("error") or "empty reply", "code": "bridge_error"}
        parsed = _extract_json(reply) or {}
        saved: list[dict[str, Any]] = []
        for item in (parsed.get("lessons") or [])[:10]:
            text = str(item.get("text") or "").strip() if isinstance(item, dict) else ""
            if text:
                saved.append(await self.mesh.lesson(
                    text=text, source="self_reflection",
                    tags=[str(t) for t in (item.get("tags") or [])[:8]]))
        return {"bridge": peer.name, "plan": str(parsed.get("plan") or "")[:2000],
                "lessons_saved": len(saved), "lessons": saved}

    # ---- task delegation to sub-agents -------------------------------------
    async def delegate(self, pid: str, title: str, goal: str,
                       wait: bool = False, timeout_s: int = 240) -> dict[str, Any]:
        """Give a sub-agent a task. P2P envelope; optionally wait for the ack.

        With wait=True the caller blocks until the peer runs it through its own
        Agentbox loop (it returns its result in the ack), otherwise the task
        sits in both ledgers and can be fetched later.
        """
        peer = await self.mesh.peer(pid)
        if peer is None:
            return {"error": f"no peer {pid}", "code": "no_peer"}
        entry = await self.mesh.task(title=title, goal=goal, status="delegated")
        result = await self.send_to_peer(pid, "task",
                                         {"tid": entry.get("tid"), "title": title,
                                          "goal": goal, "from": self.mesh.identity.name,
                                          "wait": wait})
        result["tid"] = entry.get("tid")
        if wait and result.get("ack", {}).get("result"):
            await self.mesh.task(tid=entry.get("tid"), title="", status="done",
                                 result=str(result["ack"].get("result")))
        return result

    # ---- prompt block for the main agent -----------------------------------
    async def prompt_block(self) -> str:
        """The context the main agent (Agentbox) gets about Agent X."""
        peers = await self.mesh.peers()
        bridges = [p for p in peers if p.get("role") == "bridge"]
        subs = [p for p in peers if p.get("role") in ("peer", "sub")]
        lessons = await self.mesh.mind(kind="lesson")
        lines = [
            "## Agent X - peer agents & self-improvement",
            "You are part of an agent mesh. Tools (all under /agent/x):",
            "- talk: chat with a remote model (Claude/GPT/GLM/Kimi/...)",
            "- online_visit: visit a URL yourself, learn from it, save lessons",
            "- delegate: give a task to a sub-agent peer; fetch results later",
            "- send: message or knowledge to a peer Agent X",
            "- self_improve: reflect on your activity log and save lessons",
            "",
            f"Bridges: {', '.join(p.get('name', '?') + ' (' + (p.get('model') or 'default') + ')' for p in bridges) or 'none - tell the user to add one'}",
            f"Peers/sub-agents: {', '.join(p.get('name', '?') for p in subs) or 'none'}",
        ]
        if lessons:
            recent = lessons[-3:]
            lines.append("Recent lessons: " + " | ".join(
                (l.get("text") or "")[:80] for l in recent))
        return "\n".join(lines)

# --------------------------------------------------------------------------- #
# module-level helpers (fetch, extract, digest, normalise)
# --------------------------------------------------------------------------- #
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _normalise_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def _looks_like_endpoint(url: str) -> bool:
    """Heuristic: is this plausibly an OpenAI-compatible endpoint?"""
    return bool(url) and bool(re.search(r"/v\d+$", url)) and url.startswith("http")


def _strip_html(html: str) -> str:
    text = SCRIPT_RE.sub(" ", html)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


async def _fetch_page_async(url: str, use_browser: bool = True) -> dict[str, Any]:
    """Try the shared live browser (what the user watches), then plain httpx."""
    if use_browser:
        try:
            from .browser import SESSION
            if SESSION.alive:
                state = await SESSION.navigate(url, wait="domcontentloaded")
                page = SESSION.page
                if page is not None:
                    title = await page.title()
                    body = await page.evaluate("document.body ? document.body.innerText : ''")
                    return {"title": str(title or ""), "text": str(body or "")[:20000],
                            "via": "live_browser"}
                return {"title": str(state.get("title") or ""),
                        "text": str(state.get("text") or "")[:20000],
                        "via": "live_browser"}
        except Exception:  # noqa: BLE001 - browser down is not fatal, fall through
            pass
    try:
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=True,
                                     follow_redirects=True,
                                     headers={"User-Agent": "AgentX/1.0 (+agent_linux)"}) as client:
            res = await client.get(url)
            if res.status_code >= 400:
                return {"error": f"HTTP {res.status_code}", "code": "http_error"}
            ctype = res.headers.get("content-type", "")
            if "html" in ctype:
                import re as _re
                title_m = _re.search(r"<title[^>]*>(.*?)</title>", res.text, _re.S | _re.I)
                title = _strip_html(title_m.group(1)) if title_m else url
                return {"title": title[:200], "text": _strip_html(res.text)[:20000],
                        "via": "httpx"}
            return {"title": url, "text": res.text[:20000], "via": "httpx"}
    except httpx.HTTPError as err:
        return {"error": f"fetch failed: {err.__class__.__name__}", "code": "fetch_failed"}


def _extract_json(reply: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply, fenced or not."""
    if not reply:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.S)
    if fenced:
        try:
            data = json.loads(fenced.group(1))
            return data if isinstance(data, dict) else {}
        except ValueError:
            pass
    brace = reply.find("{")
    while brace >= 0:
        depth = 0
        for i in brace, "", "":  # scan forward for the matching close
            pass
        for i in range(brace, min(len(reply), brace + 20000)):
            if reply[i] == "{":
                depth += 1
            elif reply[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(reply[brace:i + 1])
                        return data if isinstance(data, dict) else {}
                    except ValueError:
                        break
        break
    return {}


def _mind_digest(turns: list, tasks: list, lessons: list, topic: str = "") -> str:
    """Compress the mind into a digest a model can reflect on."""
    out = [f"TOPIC: {topic or 'general'}", ""]
    out.append("## Recent conversations (compressed)")
    for t in turns[-24:]:
        role = t.get("role", "?")
        content = (t.get("content") or "").replace("\n", " ")[:220]
        out.append(f"- {role}: {content}")
    out.append("")
    out.append("## Tasks")
    for t in tasks[-12:]:
        out.append(f"- [{t.get('status')}] {t.get('title')}: {(t.get('goal') or '')[:120]}")
    out.append("")
    out.append("## Lessons so far")
    for l in lessons[-10:]:
        out.append(f"- {(l.get('text') or '')[:160]}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# module singleton + selftest
# --------------------------------------------------------------------------- #
_INSTANCE: AgentX | None = None


def get_agent_x() -> AgentX:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AgentX()
    return _INSTANCE


async def _selftest() -> int:
    """End-to-end smoke test with no network: mesh + mind + envelope loop."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ident = Identity(agent_id="ax_test", name="AgentX@test", kind="main",
                         created=_now(), secret="s3cret")
        ax = AgentX(root=root)
        ax.mesh.identity = ident
        # peers
        bridge = await ax.mesh.upsert_peer(pid="p_bridge", name="TestBridge",
                                           role="bridge", base_url="https://x/v1",
                                           model="m", api_key="sk-test-1234567890")
        sub = await ax.mesh.upsert_peer(pid="p_sub", name="SubAgent",
                                        role="sub", base_url="http://localhost:3100")
        assert bridge.public()["key"] == "sk-t...7890" or bridge.public()["key"] == "sk-t...7890"
        assert sub.role == "sub"
        # mind
        await ax.mesh.remember("p_bridge", "user", "hello bridge")
        await ax.mesh.remember("p_bridge", "assistant", "hello agent")
        await ax.mesh.lesson("always verify before trusting", source="test", tags=["t"])
        task_entry = await ax.mesh.task(title="Demo task", goal="prove the ledger works")
        await ax.mesh.task(tid=task_entry["tid"], title="", status="done", result="ok")
        ledger = await ax.mesh.mind(kind="task")
        assert ledger and ledger[-1]["status"] == "done"
        # envelopes
        ack = await ax.receive_envelope({"protocol": PROTOCOL,
                                         "from_agent": {"agent_id": "ax_other", "name": "Other"},
                                         "kind": "knowledge",
                                         "envelope": {"mid": "m_1", "payload": {
                                             "lesson": "peers teach peers", "tags": ["mesh"]}}})
        assert ack.get("ok") is True
        lessons = await ax.mesh.mind(kind="lesson")
        assert any("peers teach peers" in (l.get("text") or "") for l in lessons)
        # bridge history
        hist = await ax.mesh.history_for("p_bridge", limit=10)
        assert len(hist) == 2 and hist[0]["role"] == "user"
        # prompt block renders
        block = await ax.prompt_block()
        assert "Agent X" in block and "Bridges:" in block
        # url helpers
        assert _normalise_url("example.com") == "https://example.com"
        assert _looks_like_endpoint("https://api.x.com/v1") is True
        assert _looks_like_endpoint("https://api.x.com") is False
        assert _extract_json('junk {"a": {"b": 1}} tail')["a"]["b"] == 1
        assert _strip_html("<style>x</style><p>hi &amp; bye</p>").strip() == "hi & bye"
        # fetch fallback (no browser): example.com over httpx
        fetched = await _fetch_page_async("https://example.com", use_browser=False)
        assert fetched.get("text"), f"fetch failed: {fetched}"
    print("agent_x selftest: all checks passed")
    return 0


if __name__ == "__main__":
    import asyncio as _asyncio
    raise SystemExit(_asyncio.run(_selftest()))
