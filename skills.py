"""Skills — uploadable instructions the agent can load on demand.

A skill is a folder with a `SKILL.md`: frontmatter (`name`, `description`, and
optionally `when_to_use`) plus a body of instructions, exactly like the skill
format Claude/Operit use. Anything else in the folder — scripts, templates,
reference docs — ships with it.

The important design decision is **progressive disclosure**. Dumping every
skill's body into the system prompt would burn the context window on a project
with ten skills, so the agent only ever sees a one-line index:

    <skills>
    - pdf-forms: Fill and flatten PDF forms. (read it with read_skill)
    </skills>

…and calls `read_skill` to load a body, `list_skill_files` + `read_skill_file`
to open what ships with it. That keeps a large skill library free until used.

Uploads arrive as either a `.md` file (single skill) or a `.zip` (a bundle, the
SKILL.md anywhere in it, sibling files kept). Both are stored through
`store.py`, so a Supabase/Neon deployment keeps skills in the database instead
of on a disk that a redeploy will wipe.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import zipfile
from typing import Any

from .store import StoreError, get_store, valid_key

log = logging.getLogger("terminal.skills")

PREFIX = "skill/"
BODY_LIMIT = 60000              # one skill body, as handed to the model
FILE_LIMIT = 200000             # one attached file
BUNDLE_LIMIT = 64               # files per skill
UNZIPPED_LIMIT = 8 * 1024 * 1024

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class SkillError(RuntimeError):
    code = "skill_error"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """`--- key: value ---` at the top, then the body.

    Deliberately not a YAML dependency: skills use flat scalar keys, and a
    hundred-line parser for six keys would be the wrong trade.
    """
    match = FRONTMATTER_RE.match(text or "")
    if not match:
        return {}, (text or "")
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        if value.lower() in ("true", "false"):
            meta[key.strip().lower()] = value.lower() == "true"
        else:
            meta[key.strip().lower()] = value
    return meta, (text or "")[match.end():]


def derive_name(meta: dict[str, Any], fallback: str) -> str:
    raw = str(meta.get("name") or fallback or "").strip().lower()
    raw = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    if not raw or not NAME_RE.match(raw):
        raise SkillError("a skill needs a name: use the frontmatter `name:` field, "
                         "or upload a file whose name is a valid id")
    return raw


def _doc_key(name: str) -> str:
    return f"{PREFIX}{valid_key(name)}"


def _public(skill: dict[str, Any], with_body: bool = False) -> dict[str, Any]:
    out = {
        "name": skill["name"],
        "description": skill.get("description", ""),
        "when_to_use": skill.get("when_to_use", ""),
        "source": skill.get("source", "upload"),
        "bytes": len(skill.get("body", "")),
        "files": sorted((skill.get("files") or {}).keys()),
        "added_at": skill.get("added_at"),
        "enabled": bool(skill.get("enabled", True)),
    }
    if with_body:
        out["body"] = skill.get("body", "")
        out["meta"] = skill.get("meta") or {}
    return out


def from_markdown(text: str, filename: str = "", source: str = "upload") -> dict[str, Any]:
    """One `.md` upload → a skill document."""
    meta, body = parse_frontmatter(text)
    name = derive_name(meta, re.sub(r"\.md$", "", (filename or "").strip()))
    return {
        "name": name,
        "description": str(meta.get("description") or _first_line(body))[:400],
        "when_to_use": str(meta.get("when_to_use") or meta.get("when") or "")[:400],
        "meta": {k: v for k, v in meta.items() if k not in ("description",)},
        "body": body.strip(),
        "files": {},
        "source": source,
        "enabled": True,
        "added_at": time.time(),
    }


def _first_line(body: str) -> str:
    for line in (body or "").splitlines():
        clean = line.strip().lstrip("#").strip()
        if clean:
            return clean
    return ""


def from_zip(blob: bytes, filename: str = "", source: str = "upload") -> dict[str, Any]:
    """A `.zip` bundle → a skill document with its sibling files attached.

    The `SKILL.md` may sit at the root or one folder deep (the usual layout when
    someone zips a directory), and every other file is kept as an attachment.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as err:
        raise SkillError(f"'{filename}' is not a readable zip") from err

    total = 0
    entries: dict[str, bytes] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        # Zip-slip guard: a name with `..` or a leading `/` never becomes a path
        # we later write — but refuse it here so it never even gets stored.
        clean = info.filename.replace("\\", "/").lstrip("/")
        if not clean or ".." in clean.split("/"):
            continue
        total += info.file_size
        if total > UNZIPPED_LIMIT:
            raise SkillError("the bundle is larger than 8 MB")
        entries[clean] = archive.read(info)
        if len(entries) > BUNDLE_LIMIT:
            raise SkillError(f"a bundle may hold at most {BUNDLE_LIMIT} files")

    main = ""
    for candidate in ("SKILL.md", "skill.md", "README.md"):
        for path in entries:
            if path == candidate or path.endswith("/" + candidate):
                if not main or path.count("/") < main.count("/"):
                    main = path
    if not main:
        raise SkillError("the bundle has no SKILL.md — add one, or upload a plain .md")

    text = entries.pop(main).decode("utf-8", "replace")
    meta, body = parse_frontmatter(text)
    folder = main.rsplit("/", 1)[0] if "/" in main else ""
    name = derive_name(meta, folder or re.sub(r"\.zip$", "", filename or ""))

    files: dict[str, str] = {}
    for path, raw in entries.items():
        relative = path[len(folder) + 1:] if folder and path.startswith(folder + "/") else path
        if not relative or relative.startswith("."):
            continue
        try:
            files[relative] = raw.decode("utf-8")
        except UnicodeDecodeError:
            files[relative] = f"<binary file, {len(raw)} bytes>"

    return {
        "name": name,
        "description": str(meta.get("description") or _first_line(body))[:400],
        "when_to_use": str(meta.get("when_to_use") or meta.get("when") or "")[:400],
        "meta": {k: v for k, v in meta.items() if k not in ("description",)},
        "body": body.strip(),
        "files": files,
        "source": source,
        "enabled": True,
        "added_at": time.time(),
    }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


async def load_skills() -> dict[str, dict[str, Any]]:
    try:
        docs = await get_store().list(PREFIX)
    except StoreError as err:
        log.warning("skills: store unavailable (%s)", err)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for doc in docs:
        name = doc.get("name")
        if isinstance(name, str) and NAME_RE.match(name):
            out[name] = doc
    return out


async def get_skill(name: str) -> dict[str, Any] | None:
    return (await load_skills()).get((name or "").strip().lower())


async def save_skill(skill: dict[str, Any]) -> dict[str, Any]:
    name = str(skill.get("name") or "")
    if not NAME_RE.match(name):
        raise SkillError("invalid skill name")
    await get_store().put(_doc_key(name), skill)
    return skill


async def delete_skill(name: str) -> bool:
    return await get_store().delete(_doc_key((name or "").strip().lower()))


async def set_enabled(name: str, enabled: bool) -> dict[str, Any] | None:
    skill = await get_skill(name)
    if skill is None:
        return None
    skill["enabled"] = bool(enabled)
    await save_skill(skill)
    return skill


# --------------------------------------------------------------------------- #
# What the agent sees
# --------------------------------------------------------------------------- #


async def index_prompt() -> str:
    """The one-line-per-skill index. Empty when there are no skills.

    This is the whole cost of a large skill library: a name and a description.
    """
    skills = [s for s in (await load_skills()).values() if s.get("enabled", True)]
    if not skills:
        return ""
    lines = []
    for skill in sorted(skills, key=lambda s: s["name"])[:60]:
        description = str(skill.get("description") or "").strip()
        extra = f" Files: {', '.join(sorted((skill.get('files') or {}).keys())[:6])}." if skill.get("files") else ""
        lines.append(f"- {skill['name']}: {description}{extra}")
    return (
        "You have skills available. Each is instructions you can load when the task "
        "matches — do not guess at one, read it first.\n"
        "<skills>\n" + "\n".join(lines) + "\n</skills>"
    )


async def read(name: str) -> dict[str, Any]:
    """One skill body, capped, for the `read_skill` tool."""
    skill = await get_skill(name)
    if skill is None:
        known = ", ".join(sorted(await load_skills())) or "none"
        return {"error": f"no skill '{name}' (available: {known})"}
    if not skill.get("enabled", True):
        return {"error": f"skill '{name}' is disabled"}
    body = str(skill.get("body") or "")
    out = {
        "name": skill["name"],
        "description": skill.get("description", ""),
        "instructions": body[:BODY_LIMIT],
        "files": sorted((skill.get("files") or {}).keys()),
    }
    if len(body) > BODY_LIMIT:
        out["truncated"] = True
    return out


async def read_file(name: str, path: str) -> dict[str, Any]:
    skill = await get_skill(name)
    if skill is None:
        return {"error": f"no skill '{name}'"}
    files = skill.get("files") or {}
    wanted = (path or "").strip().lstrip("/")
    if wanted not in files:
        return {"error": f"'{wanted}' is not part of skill '{name}'",
                "files": sorted(files)}
    content = str(files[wanted])
    return {"skill": name, "path": wanted, "content": content[:FILE_LIMIT],
            "truncated": len(content) > FILE_LIMIT}


async def status() -> dict[str, Any]:
    skills = await load_skills()
    return {
        "skills": len(skills),
        "enabled": sum(1 for s in skills.values() if s.get("enabled", True)),
        "names": sorted(skills),
        "files": sum(len(s.get("files") or {}) for s in skills.values()),
    }


def public(skill: dict[str, Any], with_body: bool = False) -> dict[str, Any]:
    return _public(skill, with_body)


def export_json() -> str:
    """A machine-readable schema note for the console's upload help."""
    return json.dumps({
        "skill_md": "---\nname: pdf-forms\ndescription: Fill PDF forms.\n---\n\nSteps…",
        "bundle": "a .zip containing SKILL.md plus any files it references",
    }, indent=2)