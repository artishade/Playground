"""Project Brain — the built-in, zero-key agent mind for this codebase.

Every AI model that lands on this project (Claude Code, Codex, a bridge model,
a peer agent) normally has to re-read the repository to understand it. The
Brain removes that: it carries a pre-digested PROJECT_MAP.md + MEMORY.md and a
live symbol index, and answers project questions deterministically — no API
key, no model, no network. It is the "ask before you read" layer.

    GET  /agent/brain/ask?q=how+does+key+rotation+work
    GET  /agent/brain/next                      the roadmap
    GET  /agent/brain/map                       the whole codebase map
    GET  /agent/brain/memory?section=lessons    the journal
    POST /agent/brain/remember                  teach the Brain one fact
    GET  /agent/brain/status                    index stats

Design rules (house style):
  - stdlib + fastapi only; no model calls, no outbound network
  - deterministic: same question → same answer, even with every provider down
  - failure-isolated: a missing map or a bad question never 500s the caller
  - nothing here ever echoes a secret

    python3 -m agent_linux.project_brain --selftest
"""
from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config
from .config import PACKAGE_ROOT

router = APIRouter()

BRAIN_DIRNAME = "project-brain"
MAP_FILE = "PROJECT_MAP.md"
MEMORY_FILE = "MEMORY.md"
MAX_SECTIONS_RETURNED = 4
MAX_SYMBOLS_RETURNED = 12
MAX_SNIPPET = 400

# --------------------------------------------------------------------------- #
# tiny helpers
# --------------------------------------------------------------------------- #
_STOPWORDS = frozenset(
    "a an and are as at be but by can did do does for from get got had has have "
    "how i if in is it its me my not of on or our so than that the their them "
    "then there these they this to was we what when where which who why will "
    "with you your kore korby korly keno ki".split()
)


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens, stopwords and 1-char noise dropped."""
    return [t for t in re.findall(r"[a-z0-9_]+", (text or "").lower())
            if len(t) > 1 and t not in _STOPWORDS]


def _brain_dir() -> Path:
    """The brain's files live INSIDE the package dir, so a standalone copy of
    agent_linux/ always ships with its own mind (deploy/ zips the package)."""
    return _pkg_dir() / BRAIN_DIRNAME


def _pkg_dir() -> Path:
    """The agent_linux package directory itself (where the .py files live)."""
    return Path(__file__).resolve().parent


def _read(path: Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fallback


def _split_sections(markdown: str) -> list[dict[str, str]]:
    """##-level sections of a markdown doc, with their ###-children inlined."""
    sections: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in (markdown or "").splitlines():
        if line.startswith("## "):
            if current:
                sections.append(current)
            current = {"title": line[3:].strip(), "body": []}
        elif line.startswith("### ") and current:
            current["title"] += " · " + line[4:].strip()
        elif current is not None:
            current["body"].append(line)
    if current:
        sections.append(current)
    out: list[dict[str, str]] = []
    for s in sections:
        body = "\n".join(s["body"]).strip()
        out.append({"title": s["title"], "body": body,
                    "tokens": set(_tokens(s["title"] + " " + body))})
    return out


# --------------------------------------------------------------------------- #
# the symbol index — a live, zero-cost view of the codebase
# --------------------------------------------------------------------------- #
_DEF_RE = re.compile(
    r"^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)"
    r"|^class\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_ROUTE_RE = re.compile(
    r"@(?:router|app)\.(get|post|patch|delete|put)\(\s*[\"']([^\"']+)[\"']"
)
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{3,}\s*=")


def _symbol_index() -> list[dict[str, Any]]:
    """Sweep the package's .py files for defs/classes/routes/env constants."""
    symbols: list[dict[str, Any]] = []
    try:
        files = sorted(_pkg_dir().glob("*.py"))
    except OSError:
        return symbols
    for path in files:
        module = f"agent_linux.{path.stem}"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            m = _DEF_RE.match(line)
            if m:
                name = m.group(1) or m.group(2)
                kind = "class" if m.group(2) else ("def" if line.startswith("def") else "async def")
                symbols.append({"name": name, "kind": kind, "file": module,
                                "line": lineno})
                continue
            m = _ROUTE_RE.search(line)
            if m:
                symbols.append({"name": f"{m.group(1).upper()} {m.group(2)}",
                                "kind": "route", "file": module, "line": lineno})
                continue
            if _ENV_RE.match(line) and line.isupper() is False:
                name = line.split("=")[0].strip()
                if len(name) > 4:
                    symbols.append({"name": name, "kind": "const",
                                    "file": module, "line": lineno})
    return symbols


def _match_symbols(query_tokens: list[str]) -> list[dict[str, Any]]:
    """Symbols whose names overlap the question tokens, best first."""
    qset = set(query_tokens)
    scored: list[tuple[int, dict[str, Any]]] = []
    for sym in _symbol_index():
        parts = set(_tokens(sym["name"].replace("/", " ")))
        if sym["kind"] == "route":
            parts |= set(_tokens(sym["name"]))
        overlap = parts & qset
        if overlap:
            scored.append((len(overlap), sym))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["file"], pair[1]["line"]))
    return [s for _, s in scored[:MAX_SYMBOLS_RETURNED]]


# --------------------------------------------------------------------------- #
# retrieval — score map/memory sections against the question
# --------------------------------------------------------------------------- #
def _corpus() -> list[dict[str, Any]]:
    """Every retrievable chunk: map sections, memory sections, skill readme."""
    docs: list[dict[str, Any]] = []
    for fname, source in ((MAP_FILE, "map"), (MEMORY_FILE, "memory")):
        text = _read(_brain_dir() / fname)
        for i, sec in enumerate(_split_sections(text)):
            docs.append({"title": sec["title"], "body": sec["body"],
                         "tokens": sec["tokens"], "source": source,
                         "file": f"project-brain/{fname}#section-{i}",
                         "order": i})
    # the skill's own SKILL.md, if a bundle dropped one next to the maps
    skill_md = _read(_brain_dir() / "SKILL.md")
    if skill_md:
        for i, sec in enumerate(_split_sections(skill_md)):
            docs.append({"title": sec["title"], "body": sec["body"],
                         "tokens": sec["tokens"], "source": "skill",
                         "file": "project-brain/SKILL.md", "order": i})
    return docs


def _score(doc: dict[str, Any], qtokens: list[str]) -> int:
    hits = doc["tokens"].intersection(qtokens)
    title_hits = doc["tokens"].intersection(set(qtokens))
    # title words are worth more: cheap trick, visible in the weights below
    title_bonus = len(set(_tokens(doc["title"])) & set(qtokens)) * 3
    return len(hits) + title_bonus


def _explain(question: str) -> dict[str, Any]:
    qtokens = _tokens(question)
    ranked = sorted(_corpus(), key=lambda d: _score(d, qtokens), reverse=True)
    top = [d for d in ranked if _score(d, qtokens) > 0][:MAX_SECTIONS_RETURNED]
    symbols = _match_symbols(qtokens)
    return {"qtokens": qtokens, "sections": top, "symbols": symbols}


def _answer_lines(question: str, hits: list[dict[str, Any]],
                  symbols: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for d in hits:
        snippet = d["body"][:MAX_SNIPPET].strip()
        lines.append(f"### {d['title']}  ({d['file']})\n{snippet}")
    if symbols:
        lines.append("### Code pointers\n" + "\n".join(
            f"- `{s['name']}` ({s['kind']}) — {s['file']}:{s['line']}"
            for s in symbols[:8]))
    return lines


# --------------------------------------------------------------------------- #
# memory writes — the Brain learns by append, never rewrite
# --------------------------------------------------------------------------- #
def _remember(area: str, note: str, source: str) -> dict[str, Any]:
    path = _brain_dir() / MEMORY_FILE
    text = _read(path, "# Agent_Linux (AgentX) — Project Memory\n")
    stamp = time.strftime("%Y-%m-%d")
    entry = f"| {stamp} | {(area or 'general')[:40]} | {(note or '').strip()[:400]} |"
    if "| Date | Area | What happened |" in text:
        # insert right after the fix-history table header
        text = text.replace(
            "| Date | Area | What happened |",
            "| Date | Area | What happened |\n" + entry, 1)
    else:
        text = text.rstrip() + f"\n\n## Learned ({stamp})\n\n- [{area}] {note} (source: {source})\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return {"ok": True, "saved": True, "note": note[:400]}
    except OSError as err:
        return {"ok": False, "error": f"memory write failed: {err}"}


def _next_steps() -> list[str]:
    """The roadmap straight out of MEMORY.md's 'Next steps' section."""
    text = _read(_brain_dir() / MEMORY_FILE)
    m = re.search(r"##\s*Next steps.*?\n(.*?)(?=\n##\s|\Z)", text, re.S)
    if not m:
        return []
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()][:12]


# --------------------------------------------------------------------------- #
# public engine API (also used by agent_x receive + agentbox tool)
# --------------------------------------------------------------------------- #
def brain_ask(question: str) -> dict[str, Any]:
    """Answer a project question with zero network, zero keys. Never raises."""
    q = (question or "").strip()
    if not q:
        return {"ok": False, "error": "question is empty", "code": "empty_question"}
    started = time.perf_counter()
    try:
        hits = _explain(q)
        lines = _answer_lines(q, hits["sections"], hits["symbols"])
        if not lines:
            return {
                "ok": True, "answer": "No confident match in the project map. "
                "Read the relevant file directly, or teach the Brain: "
                "POST /agent/brain/remember {area, note}.",
                "confident": False, "tokens": hits["qtokens"],
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        answer = f"**Q: {q}**\n\n" + "\n\n".join(lines)
        return {
            "ok": True, "answer": answer, "confident": True,
            "sections": [{"title": d["title"], "file": d["file"]} for d in hits["sections"]],
            "symbols": [{"name": s["name"], "kind": s["kind"],
                         "file": s["file"], "line": s["line"]}
                        for s in hits["symbols"]],
            "tokens": hits["qtokens"],
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as err:  # noqa: BLE001 — the brain never 500s
        return {"ok": False, "error": f"{err.__class__.__name__}: {err}",
                "trace": traceback.format_exc(limit=2), "code": "brain_error"}


def brain_status() -> dict[str, Any]:
    map_text = _read(_brain_dir() / MAP_FILE)
    mem_text = _read(_brain_dir() / MEMORY_FILE)
    symbols = _symbol_index()
    return {
        "enabled": True,
        "mode": "zero-key deterministic (no model, no network)",
        "brain_dir": str(_brain_dir()),
        "map_present": bool(map_text), "memory_present": bool(mem_text),
        "map_sections": len(_split_sections(map_text)),
        "memory_sections": len(_split_sections(mem_text)),
        "symbols_indexed": len(symbols),
        "next_steps": len(_next_steps()),
    }


# --------------------------------------------------------------------------- #
# HTTP surface — mounted at /agent/brain by service.py
# --------------------------------------------------------------------------- #
def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


@router.get("/health")
@router.get("/status")
async def status():
    return _json(brain_status())


@router.get("/map")
async def the_map(section: str = ""):
    text = _read(_brain_dir() / MAP_FILE)
    if not text:
        return _json({"error": "PROJECT_MAP.md missing", "code": "no_map"}, 404)
    if section:
        for sec in _split_sections(text):
            if section.lower() in sec["title"].lower():
                return _json({"title": sec["title"], "body": sec["body"]})
        return _json({"error": f"no section '{section}'",
                      "sections": [s["title"] for s in _split_sections(text)]}, 404)
    return _json({"content": text})


@router.get("/memory")
async def memory(section: str = ""):
    text = _read(_brain_dir() / MEMORY_FILE)
    if section:
        m = re.search(rf"##\s*.*{re.escape(section)}.*?\n(.*?)(?=\n##\s|\Z)",
                      text, re.S | re.I)
        if not m:
            return _json({"error": f"no section '{section}'"}, 404)
        return _json({"section": section, "content": m.group(1).strip()})
    return _json({"content": text})


@router.get("/next")
async def next_steps():
    return _json({"next_steps": _next_steps()})


@router.get("/ask")
async def ask_get(q: str = ""):
    return _json(brain_ask(q))


@router.post("/ask")
async def ask_post(request: Request):
    try:
        body = json.loads((await request.body()) or b"{}")
    except ValueError:
        body = {}
    question = str(body.get("q") or body.get("question") or "").strip()
    return _json(brain_ask(question))


@router.post("/remember")
async def remember(request: Request):
    try:
        body = json.loads((await request.body()) or b"{}")
    except ValueError:
        body = {}
    note = str(body.get("note") or "").strip()
    if not note:
        return _json({"error": "note is required", "code": "bad_request"}, 400)
    return _json(_remember(area=str(body.get("area") or "general"), note=note,
                           source=str(body.get("source") or "api")))


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    assert _tokens("How does key rotation work?") == ["key", "rotation", "work"]
    assert len(_split_sections("# t\n## A\nbody\n## B\nbody2")) == 2
    index = _symbol_index()
    assert index, "symbol index came back empty"
    names = {s["name"] for s in index}
    assert any("run_tool" in n for n in names), "run_tool not indexed"
    res = brain_ask("how does the agent chat fallback chain work")
    assert res["ok"] and res.get("confident"), f"ask failed: {res}"
    res2 = brain_ask("provider cooldown")
    assert res2["ok"]
    assert _next_steps(), "next steps extraction failed"
    print("project_brain selftest: all checks passed "
          f"({len(index)} symbols indexed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())