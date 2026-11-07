"""Recipes — captured Flow Music requests turned into reusable templates.

Flow Music has no public API, and its private calls can only be signed by a
signed-in flowmusic.google tab. Guessing payload shapes does not work, so every
operation starts as one real UI action recorded in capture mode. A recipe is
that recorded request with the user-specific values (prompt, lyrics, bpm…)
swapped for placeholders, plus rules for pulling ids and audio URLs out of the
response.

Recipe file (recipes/<name>.json):

    {
      "name": "generate_song",
      "description": "...",
      "transport": "batchexecute" | "fetch",
      "rpc":     {"path": "/_/App/data/batchexecute", "rpcid": "AbCd1", "payload": "<inner JSON>"},
      "request": {"method": "POST", "url": "/api/...", "headers": {...}, "body": "<template>"},
      "captcha_action": "MUSIC_GENERATION" | null,
      "captcha_sitekey": null,          // only if the page doesn't expose one
      "variables": {"prompt": {"required": true}, "bpm": {"default": null}},
      "extract": {"job_id": {"regex": "..."}, "lyrics": {"path": [0, 1]}},
      "poll": "poll_song" | null
    }

Placeholders: ``{{name}}`` or ``{{name|filter}}``.

    json     (default) value as JSON string *content* — for use inside quotes
    jsonval  full JSON value: 120, true, null, "text" (quotes included)
    raw      str(value) untouched
    url      percent-encoded

Built-ins: ``{{$uuid}}`` fresh uuid4, ``{{$seed}}`` random int, ``{{$now_ms}}``.
The literal ``__CAPTCHA__`` is swapped for a fresh reCAPTCHA token by the
extension right before the request leaves the page.
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse

from agent import config

CAPTCHA_SLOT = "__CAPTCHA__"

# Operations the agent knows how to drive. Anything else still runs through
# POST /api/recipes/{name}/run.
KNOWN_RECIPES = {
    "generate_song": "Create a song from prompt / lyrics (vars: prompt, lyrics, title, instrumental, bpm, duration_s)",
    "poll_song": "Check a generation job (vars: job_id). Must extract audio_urls when done",
    "get_song": "Fetch a finished track by id (vars: remote_id). Optional",
    "extend_song": "Extend / continue a track (vars: remote_id, prompt, lyrics, continue_at)",
    "cover_song": "Restyle a track keeping melody (vars: remote_id, prompt)",
    "generate_lyrics": "Write lyrics from a prompt (vars: prompt). Must extract lyrics",
    "upload_audio": "Upload a local audio file (vars: audio_b64, filename, mime_type)",
    "generate_music_video": "Flow Music's own music video (vars: remote_id, prompt)",
}

PLACEHOLDER_RE = re.compile(r"\{\{\s*(\$?[A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*([a-z]+)\s*)?\}\}")
_FILTERS = {"json", "jsonval", "raw", "url"}

# Signed audio links. Flow Music serves media off Google hosts; match on file
# extension first, then on common media path shapes.
_URL_CHARS = r"[^\s\"'\\<>\]\[,]"
AUDIO_URL_RE = re.compile(
    rf"https://{_URL_CHARS}+?\.(?:mp3|wav|m4a|aac|ogg|oga|flac|opus|weba)(?:\?{_URL_CHARS}*)?(?={_URL_CHARS.replace('[^', '[')}|$)",
    re.IGNORECASE,
)
AUDIO_PATH_RE = re.compile(rf"https://{_URL_CHARS}*?/(?:audio|music|track|song)s?/{_URL_CHARS}+", re.IGNORECASE)
IMAGE_URL_RE = re.compile(rf"https://{_URL_CHARS}+?\.(?:jpe?g|png|webp)(?:\?{_URL_CHARS}*)?", re.IGNORECASE)
VIDEO_URL_RE = re.compile(rf"https://{_URL_CHARS}+?\.(?:mp4|webm|mov)(?:\?{_URL_CHARS}*)?", re.IGNORECASE)
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
ERROR_RE = re.compile(r"\b(PUBLIC_ERROR_[A-Z_]+|UNUSUAL_ACTIVITY|RESOURCE_EXHAUSTED|PERMISSION_DENIED|UNAUTHENTICATED|QUOTA_EXCEEDED)\b")
# reCAPTCHA Enterprise tokens: long url-safe strings that start 03A / 0cA.
CAPTCHA_TOKEN_RE = re.compile(r"0[3c]A[A-Za-z0-9_\-]{200,}")


class RecipeError(ValueError):
    """A recipe is missing, malformed, or can't be rendered."""


# ─── Rendering ───────────────────────────────────────────────


def _builtin(name: str) -> Any:
    if name == "$uuid":
        return str(uuid.uuid4())
    if name == "$seed":
        return random.randint(1, 2_147_483_647)
    if name == "$now_ms":
        return int(time.time() * 1000)
    raise RecipeError(f"unknown built-in {name}")


def apply_filter(value: Any, filt: str) -> str:
    if filt == "jsonval":
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if filt == "raw":
        return text
    if filt == "url":
        return quote(text, safe="")
    # json: escaped string content, no surrounding quotes
    return json.dumps(text, ensure_ascii=False)[1:-1]


def render_template(template: str, values: dict[str, Any], spec: Optional[dict] = None) -> str:
    """Fill ``{{var|filter}}`` placeholders. Raises RecipeError on missing vars."""
    spec = spec or {}

    def _sub(m: re.Match) -> str:
        name, filt = m.group(1), (m.group(2) or "json")
        if filt not in _FILTERS:
            raise RecipeError(f"unknown filter '{filt}' on {{{{{name}}}}}")
        if name.startswith("$"):
            return apply_filter(_builtin(name), filt)
        if name in values and values[name] is not None:
            return apply_filter(values[name], filt)
        var_spec = spec.get(name, {})
        if var_spec.get("required"):
            raise RecipeError(f"missing required variable '{name}'")
        if name in values or "default" in var_spec:
            return apply_filter(values.get(name, var_spec.get("default")), filt)
        raise RecipeError(f"missing variable '{name}' (no default in recipe)")

    return PLACEHOLDER_RE.sub(_sub, template)


def template_variables(template: str) -> list[str]:
    seen = []
    for m in PLACEHOLDER_RE.finditer(template or ""):
        if not m.group(1).startswith("$") and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


# ─── Recipe model ────────────────────────────────────────────


@dataclass
class Recipe:
    name: str
    transport: str
    description: str = ""
    rpc: dict = field(default_factory=dict)
    request: dict = field(default_factory=dict)
    captcha_action: Optional[str] = None
    captcha_sitekey: Optional[str] = None
    variables: dict = field(default_factory=dict)
    extract: dict = field(default_factory=dict)
    poll: Optional[str] = None
    notes: list = field(default_factory=list)
    source_capture_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Recipe":
        if not isinstance(data, dict):
            raise RecipeError("recipe must be a JSON object")
        name = data.get("name")
        if not name or not re.fullmatch(r"[a-z0-9_]+", name):
            raise RecipeError("recipe name must match [a-z0-9_]+")
        transport = data.get("transport")
        if transport not in ("batchexecute", "fetch"):
            raise RecipeError("transport must be 'batchexecute' or 'fetch'")
        recipe = cls(
            name=name,
            transport=transport,
            description=data.get("description", ""),
            rpc=data.get("rpc") or {},
            request=data.get("request") or {},
            captcha_action=data.get("captcha_action"),
            captcha_sitekey=data.get("captcha_sitekey"),
            variables=data.get("variables") or {},
            extract=data.get("extract") or {},
            poll=data.get("poll"),
            notes=data.get("notes") or [],
            source_capture_id=data.get("source_capture_id"),
        )
        recipe.validate()
        return recipe

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "description": self.description,
            "transport": self.transport,
            "captcha_action": self.captcha_action,
            "captcha_sitekey": self.captcha_sitekey,
            "variables": self.variables,
            "extract": self.extract,
            "poll": self.poll,
            "notes": self.notes,
            "source_capture_id": self.source_capture_id,
        }
        if self.transport == "batchexecute":
            out["rpc"] = self.rpc
        else:
            out["request"] = self.request
        return out

    @property
    def template(self) -> str:
        if self.transport == "batchexecute":
            return self.rpc.get("payload", "")
        return self.request.get("body") or ""

    def validate(self) -> None:
        if self.transport == "batchexecute":
            for key in ("path", "rpcid", "payload"):
                if not self.rpc.get(key):
                    raise RecipeError(f"batchexecute recipe needs rpc.{key}")
        else:
            if not self.request.get("url"):
                raise RecipeError("fetch recipe needs request.url")
        for name, ex in self.extract.items():
            if not isinstance(ex, dict) or not ({"regex", "path"} & set(ex)):
                raise RecipeError(f"extract.{name} needs 'regex' or 'path'")
            if "regex" in ex:
                try:
                    re.compile(ex["regex"])
                except re.error as e:
                    raise RecipeError(f"extract.{name}: bad regex: {e}") from e
        # Every placeholder must be declared so missing values fail early.
        for var in template_variables(self.template) + template_variables(self.request.get("url", "")):
            self.variables.setdefault(var, {"required": True})

    def build_command(self, values: dict[str, Any]) -> dict:
        """Render into the params the extension executes."""
        values = dict(values or {})
        if self.transport == "batchexecute":
            inner = render_template(self.rpc["payload"], values, self.variables)
            try:
                json.loads(inner.replace(CAPTCHA_SLOT, "x"))
            except ValueError as e:
                raise RecipeError(f"rendered payload is not valid JSON: {e}") from e
            freq = json.dumps([[[self.rpc["rpcid"], inner, None, "generic"]]], separators=(",", ":"))
            return {
                "method": "batch_rpc",
                "params": {
                    "path": self.rpc["path"],
                    "rpcid": self.rpc["rpcid"],
                    "freq": freq,
                    "captchaAction": self.captcha_action,
                    "captchaSitekey": self.captcha_sitekey,
                },
            }
        req = self.request
        body = req.get("body")
        return {
            "method": "page_fetch",
            "params": {
                "url": render_template(req["url"], values, self.variables),
                "method": req.get("method", "POST"),
                "headers": req.get("headers") or {},
                "body": render_template(body, values, self.variables) if body is not None else None,
                "captchaAction": self.captcha_action,
                "captchaSitekey": self.captcha_sitekey,
            },
        }


# ─── Storage ─────────────────────────────────────────────────


def recipes_dir() -> Path:
    config.RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    return config.RECIPES_DIR


def recipe_path(name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_]+", name or ""):
        raise RecipeError("recipe name must match [a-z0-9_]+")
    return recipes_dir() / f"{name}.json"


def load_recipe(name: str) -> Recipe:
    path = recipe_path(name)
    if not path.exists():
        raise RecipeError(
            f"recipe '{name}' not found — record it with /fm-capture "
            f"(POST /api/recipes/from-capture)"
        )
    try:
        return Recipe.from_dict(json.loads(path.read_text()))
    except json.JSONDecodeError as e:
        raise RecipeError(f"recipe '{name}' is not valid JSON: {e}") from e


def has_recipe(name: str) -> bool:
    try:
        return recipe_path(name).exists()
    except RecipeError:
        return False


def save_recipe(recipe: Recipe) -> Path:
    recipe.validate()
    path = recipe_path(recipe.name)
    path.write_text(json.dumps(recipe.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return path


def list_recipes() -> list[dict]:
    out = []
    for path in sorted(recipes_dir().glob("*.json")):
        try:
            r = Recipe.from_dict(json.loads(path.read_text()))
            out.append({
                "name": r.name, "transport": r.transport, "description": r.description,
                "captcha_action": r.captcha_action, "variables": sorted(r.variables),
                "extract": sorted(r.extract), "poll": r.poll, "valid": True,
            })
        except (RecipeError, ValueError) as e:
            out.append({"name": path.stem, "valid": False, "error": str(e)})
    return out


# ─── Responses ───────────────────────────────────────────────


def decode_batchexecute(text: str) -> list[dict]:
    """Parse a batchexecute response into ``[{rpcid, data, error}]``.

    The body is ``)]}'`` then length-prefixed JSON chunks. Each ``wrb.fr``
    entry carries its payload as a JSON *string*, decoded here as well.
    """
    if not text:
        return []
    body = text
    if body.startswith(")]}'"):
        body = body[4:]
    decoder = json.JSONDecoder()
    out, i, n = [], 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "[":
            try:
                chunk, end = decoder.raw_decode(body, i)
            except ValueError:
                i += 1
                continue
            i = end
            for entry in chunk if isinstance(chunk, list) else []:
                if not isinstance(entry, list) or not entry:
                    continue
                if entry[0] == "wrb.fr":
                    payload = entry[2] if len(entry) > 2 else None
                    data = payload
                    if isinstance(payload, str):
                        try:
                            data = json.loads(payload)
                        except ValueError:
                            data = payload
                    err = None
                    if payload is None and len(entry) > 5 and entry[5] is not None:
                        err = entry[5]
                    out.append({"rpcid": entry[1], "data": data, "error": err})
                elif entry[0] == "er":
                    out.append({"rpcid": None, "data": None, "error": entry[1:]})
        else:
            i += 1
    return out


def _unescape(text: str) -> str:
    return (
        text.replace("\\u003d", "=").replace("\\u0026", "&")
        .replace("\\u003c", "<").replace("\\u003e", ">")
        .replace("\\/", "/").replace('\\"', '"')
    )


def response_views(text: str) -> tuple[Any, str]:
    """Return (structured data or None, searchable text).

    The searchable text is the raw body plus a JSON dump of every decoded
    payload, with common escapes undone, so one regex works for both REST and
    batchexecute responses.
    """
    data: Any = None
    parts = [text or ""]
    decoded = decode_batchexecute(text or "")
    if decoded:
        data = decoded[0]["data"] if len(decoded) == 1 else [d["data"] for d in decoded]
        parts.append(json.dumps([d["data"] for d in decoded], ensure_ascii=False, separators=(",", ":")))
    else:
        stripped = (text or "").lstrip()
        if stripped.startswith(")]}'"):
            stripped = stripped[4:].lstrip()
        if stripped[:1] in ("{", "["):
            try:
                data = json.loads(stripped)
                parts.append(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            except ValueError:
                pass
    return data, _unescape("\n".join(parts))


def _walk(data: Any, path: list) -> Any:
    cur = data
    for key in path:
        if isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        elif isinstance(cur, dict) and str(key) in cur:
            cur = cur[str(key)]
        else:
            return None
    return cur


def _dedupe(items: list) -> list:
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True) if not isinstance(item, str) else item
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _clean_url(url: str) -> str:
    return url.rstrip(".);")


def extract(recipe: Optional[Recipe], text: str) -> dict:
    """Run a recipe's extractors plus the built-in heuristics over a response.

    Built-in keys (always present): ``audio_urls``, ``image_urls``,
    ``video_urls``, ``uuids``, ``errors``. A recipe extractor with the same name
    overrides the heuristic.
    """
    data, haystack = response_views(text)
    audio = [_clean_url(u) for u in AUDIO_URL_RE.findall(haystack)]
    if not audio:
        audio = [_clean_url(u) for u in AUDIO_PATH_RE.findall(haystack)]
    result: dict[str, Any] = {
        "audio_urls": _dedupe(audio),
        "image_urls": _dedupe([_clean_url(u) for u in IMAGE_URL_RE.findall(haystack)]),
        "video_urls": _dedupe([_clean_url(u) for u in VIDEO_URL_RE.findall(haystack)]),
        "uuids": _dedupe(UUID_RE.findall(haystack))[:50],
        "errors": _dedupe(ERROR_RE.findall(text or "")),
    }
    for d in decode_batchexecute(text or ""):
        if d.get("error") is not None:
            result["errors"].append(f"RPC_ERROR {json.dumps(d['error'])[:200]}")

    if recipe is None:
        return result
    for name, ex in recipe.extract.items():
        if "path" in ex:
            value = _walk(data, ex["path"])
        else:
            matches = list(re.finditer(ex["regex"], haystack, re.DOTALL))
            group = ex.get("group", 1 if re.compile(ex["regex"]).groups else 0)
            values = [m.group(group) for m in matches]
            if ex.get("all"):
                value = _dedupe(values)
            else:
                value = values[0] if values else None
        if isinstance(value, str) and ex.get("unescape_json"):
            try:
                value = json.loads(f'"{value}"')
            except ValueError:
                pass
        result[name] = value
    return result


# ─── Capture → recipe ────────────────────────────────────────


def _escaped_forms(value: Any, body_kind: str) -> list[str]:
    text = value if isinstance(value, str) else str(value)
    if body_kind == "form":
        return [quote(text, safe=""), quote(text, safe="").replace("%20", "+")]
    if body_kind in ("json", "batch"):
        return [json.dumps(text, ensure_ascii=False)[1:-1], json.dumps(text)[1:-1]]
    return [text]


def _body_kind(content_type: str, body: str) -> str:
    ct = (content_type or "").lower()
    if "json" in ct:
        return "json"
    if "x-www-form-urlencoded" in ct:
        return "form"
    if body and body.lstrip()[:1] in ("{", "["):
        return "json"
    return "raw"


def parse_batch_capture(url: str, body: str) -> dict:
    """Pull rpcid / path / inner payload out of a captured batchexecute POST."""
    form = parse_qs(body or "", keep_blank_values=True)
    freq_list = form.get("f.req")
    if not freq_list:
        raise RecipeError("captured batchexecute body has no f.req")
    try:
        envelope = json.loads(freq_list[0])
        calls = envelope[0]
    except (ValueError, IndexError, TypeError) as e:
        raise RecipeError(f"can't parse f.req: {e}") from e
    if not calls:
        raise RecipeError("f.req holds no RPC call")
    first = calls[0]
    return {
        "path": urlparse(url).path,
        "rpcid": first[0],
        "payload": first[1],
        "rpc_count": len(calls),
        "rpcids": [c[0] for c in calls],
    }


def recipe_from_capture(
    capture: dict,
    name: str,
    sample_values: dict[str, Any],
    *,
    description: str = "",
    captcha_action: Optional[str] = None,
    extract_rules: Optional[dict] = None,
    poll: Optional[str] = None,
    rpc_index: int = 0,
) -> tuple[Recipe, list[str]]:
    """Template a captured request.

    ``sample_values`` maps variable name → the literal value you typed into the
    UI during the capture, e.g. ``{"prompt": "dreamy lofi", "bpm": 84}``. Each
    literal must occur in the request body exactly once (strings are matched in
    their escaped form). Returns the recipe and human-readable notes to review.
    """
    notes: list[str] = []
    url = capture["url"]
    body = capture.get("request_body") or ""
    is_batch = "/batchexecute" in urlparse(url).path

    if is_batch:
        parsed = parse_batch_capture(url, body)
        if parsed["rpc_count"] > 1:
            notes.append(f"f.req bundles {parsed['rpc_count']} calls {parsed['rpcids']}; using index {rpc_index}")
            envelope = json.loads(parse_qs(body)["f.req"][0])
            parsed["rpcid"], parsed["payload"] = envelope[0][rpc_index][0], envelope[0][rpc_index][1]
        template = parsed["payload"]
        kind = "batch"
    else:
        template = body
        kind = _body_kind(capture.get("request_content_type", ""), body)

    if "{{" in template:
        notes.append("captured body already contains '{{' — check placeholders by hand")

    variables: dict[str, dict] = {}
    for var, sample in (sample_values or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
            raise RecipeError(f"bad variable name '{var}'")
        if isinstance(sample, bool) or sample is None:
            raise RecipeError(f"'{var}': sample must be a string or number you typed in the UI")
        if isinstance(sample, (int, float)):
            pattern = re.compile(rf"(?<![\w.]){re.escape(json.dumps(sample))}(?![\w.])")
            hits = pattern.findall(template)
            if len(hits) != 1:
                raise RecipeError(
                    f"'{var}': number {sample} occurs {len(hits)} times in the body — "
                    "must be exactly once (pick a more distinctive value and recapture)"
                )
            template = pattern.sub(f"{{{{{var}|jsonval}}}}", template)
            variables[var] = {"default": None, "type": "number"}
            continue
        if len(sample) < 3:
            raise RecipeError(f"'{var}': sample '{sample}' is too short to locate safely")
        placed = False
        for form in _escaped_forms(sample, kind):
            count = template.count(form)
            if count == 1:
                filt = "url" if kind == "form" else ("json" if kind in ("json", "batch") else "raw")
                ph = f"{{{{{var}}}}}" if filt == "json" else f"{{{{{var}|{filt}}}}}"
                template = template.replace(form, ph)
                placed = True
                break
            if count > 1:
                raise RecipeError(f"'{var}': sample occurs {count} times in the body — must be exactly once")
        if not placed:
            raise RecipeError(f"'{var}': sample value not found in the captured body")
        variables[var] = {"required": var in ("prompt", "job_id", "remote_id")}

    tokens = CAPTCHA_TOKEN_RE.findall(template)
    if tokens:
        for tok in set(tokens):
            template = template.replace(tok, CAPTCHA_SLOT)
        if not captcha_action:
            notes.append(
                "a reCAPTCHA token was found and replaced with __CAPTCHA__, but no "
                "captcha_action was given — set it, or the request will be rejected"
            )
    elif captcha_action:
        notes.append("captcha_action set but no token was found in the body — double check")

    leftover_uuids = sorted(set(UUID_RE.findall(template)))
    if leftover_uuids:
        notes.append(
            "UUIDs left as constants (fine for a project/workspace id, wrong for a "
            f"per-request id — swap those for {{{{$uuid}}}}): {leftover_uuids[:8]}"
        )

    if is_batch:
        recipe = Recipe(name=name, transport="batchexecute",
                        rpc={"path": parsed["path"], "rpcid": parsed["rpcid"], "payload": template})
    else:
        parsed_url = urlparse(url)
        rel = parsed_url.path + (f"?{parsed_url.query}" if parsed_url.query else "")
        page_host = urlparse(capture.get("page_url") or "").netloc
        target = rel if (not page_host or parsed_url.netloc == page_host) else url
        headers = {}
        if capture.get("request_content_type"):
            headers["content-type"] = capture["request_content_type"]
        recipe = Recipe(name=name, transport="fetch",
                        request={"method": capture.get("method", "POST"), "url": target,
                                 "headers": headers, "body": template if body else None})
    recipe.description = description or KNOWN_RECIPES.get(name, "")
    recipe.captcha_action = captcha_action
    recipe.variables = variables
    recipe.extract = extract_rules or {}
    recipe.poll = poll
    recipe.notes = notes
    recipe.source_capture_id = capture.get("id")
    recipe.validate()
    return recipe, notes
