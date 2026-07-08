"""Sandboxed rendering with targeting-resolved includes.

The render path is pure: given an :class:`EnvSnapshot`, flags, variables, and a
:class:`ContentProvider`, it resolves the prompt (and every ``{% include %}``)
through the evaluator with the *same* flag context, compiles under a Jinja
``SandboxedEnvironment`` with ``StrictUndefined``, and renders.

Compiled templates are cached by blob hash (immutable content ⇒ immutable
bytecode), so the hot path never recompiles. Include resolution happens at render
time via a ``get_template`` override; a per-render include stack backstops cycles
and enforces the depth limit (static validation is the primary cycle guard).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from jinja2 import StrictUndefined, Template, Undefined
from jinja2.exceptions import TemplateNotFound, UndefinedError
from jinja2.exceptions import TemplateError as JinjaTemplateError
from jinja2.sandbox import SandboxedEnvironment

from .errors import IncludeCycle, IncludeDepthExceeded, MissingVariable, RenderError
from .evaluate import Skip, resolve
from .model import ContentProvider, EnvSnapshot, Resolution
from .variables import extract

DEPTH_LIMIT = 32


@dataclass
class RenderResult:
    text: str
    root: Resolution
    contributions: dict[str, Resolution]  # prompt_id -> Resolution (incl. root)
    content_fallback: bool
    skips: list[Skip] = field(default_factory=list)


@dataclass
class _RenderCtx:
    snapshot: EnvSnapshot
    flags: Mapping[str, Any]
    content: ContentProvider
    contributions: dict[str, Resolution] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    fallback: bool = False
    # §9 pin replay: prompt_id -> (version, commit). When present, that prompt
    # resolves to the pinned SHA directly, bypassing all targeting.
    pin: Mapping[str, tuple[int, str]] = field(default_factory=dict)


def _pinned(ctx: _RenderCtx, prompt_id: str) -> Resolution | None:
    p = ctx.pin.get(prompt_id)
    if p is None:
        return None
    version, commit = p
    return Resolution(prompt_id, version, commit, "sha", "pin", None, None)


_current: contextvars.ContextVar[_RenderCtx | None] = contextvars.ContextVar(
    "incant_render_ctx", default=None
)


def _fetch(ctx: _RenderCtx, prompt_id: str, res: Resolution):
    """Fetch a resolution's content, applying the §10 within-version fallback.

    The evaluator's ``servable`` predicate only knows about *validation*; the real
    §10 trigger is content that is validated but unfetchable (cache lost + store
    unreachable), which surfaces here as ``KeyError``. For a *live* resolution we
    then serve the newest previous-live SHA whose content IS available, flagging
    ``content_fallback``. Pinned SHA / tip resolutions never degrade.

    Returns ``(blob, resolution)`` — the resolution updated to the served SHA.
    """
    try:
        return ctx.content.get(prompt_id, res.version, res.commit), res
    except KeyError:
        if res.at != "live":
            raise
        for row in ctx.snapshot.versions.get(prompt_id, {}).values():
            if row.version != res.version:
                continue
            for sha in row.previous_live:
                if sha == res.commit or not ctx.snapshot.servable(prompt_id, sha):
                    continue
                try:
                    blob = ctx.content.get(prompt_id, res.version, sha)
                except KeyError:
                    continue
                ctx.fallback = True
                return blob, replace(res, commit=sha, content_fallback=True)
        raise


class _IncantEnvironment(SandboxedEnvironment):
    """A sandboxed environment whose includes resolve through the targeting engine."""

    def _resolve_include(self, name: str) -> Template:
        ctx = _current.get()
        if ctx is None:  # pragma: no cover - defensive
            raise TemplateNotFound(name)
        res = _pinned(ctx, name) or resolve(ctx.snapshot, name, ctx.flags, skips=ctx.skips)
        if res.content_fallback:
            ctx.fallback = True
        blob, res = _fetch(ctx, name, res)  # §10 within-version fallback
        ctx.contributions[name] = res
        base = _compile(self, blob.blob_sha, blob.source, name)
        return _stack_wrapped(base, name)

    def get_template(self, name, parent=None, globals=None):  # type: ignore[override]
        if isinstance(name, Template):
            return name
        return self._resolve_include(str(name))

    def get_or_select_template(self, template_name_or_list, parent=None, globals=None):  # type: ignore[override]
        if isinstance(template_name_or_list, (list, tuple)):
            template_name_or_list = template_name_or_list[0]
        return self.get_template(template_name_or_list, parent, globals)


# The single shared environment. Autoescape off (plain text for LLMs); no
# filesystem loader — content only ever arrives through the ContentProvider.
_ENV = _IncantEnvironment(
    undefined=StrictUndefined,  # strict everywhere; optional vars are injected lenient
    autoescape=False,
    cache_size=0,  # we maintain our own blob-keyed compiled cache
    auto_reload=False,
    keep_trailing_newline=True,
)

# Blob-keyed compiled-template cache: immutable content ⇒ never invalidated.
_COMPILED: dict[str, Template] = {}
_COMPILE_ORDER: list[str] = []
_CACHE_MAX = 4096
compile_misses = 0

# Blob-keyed variable-extraction cache (parsing is deterministic per blob).
_EXTRACT: dict[str, Any] = {}
_EXTRACT_ORDER: list[str] = []


def _extract_cached(blob_sha: str, source: str):
    ev = _EXTRACT.get(blob_sha)
    if ev is not None:
        return ev
    ev = extract(source)
    _EXTRACT[blob_sha] = ev
    _EXTRACT_ORDER.append(blob_sha)
    if len(_EXTRACT_ORDER) > _CACHE_MAX:
        _EXTRACT.pop(_EXTRACT_ORDER.pop(0), None)
    return ev


def _closure_optionals(ctx: "_RenderCtx", prompt_id: str, source: str, blob_sha: str) -> set[str]:
    """Names that are optional across the whole include closure (and required
    nowhere) — the set to render leniently. Required-anywhere wins over optional.

    Walking the closure mirrors the render's own targeting-resolved includes, so
    a fragment's guarded-optional variable is treated leniently too.
    """

    required: set[str] = set()
    optional: set[str] = set()
    seen: set[str] = set()

    def walk(pid: str, src: str, bsha: str) -> None:
        if pid in seen:
            return
        seen.add(pid)
        ev = _extract_cached(bsha, src)
        required.update(ev.required)
        optional.update(ev.optional)
        for inc in ev.includes:
            try:
                res = resolve(ctx.snapshot, inc, ctx.flags)
                blob = ctx.content.get(inc, res.version, res.commit)
            except Exception:
                continue  # unresolved/unservable include — the render will surface it
            walk(inc, blob.source, blob.blob_sha)

    walk(prompt_id, source, blob_sha)
    return optional - required


def _compile(env: SandboxedEnvironment, blob_sha: str, source: str, name: str) -> Template:
    global compile_misses
    cached = _COMPILED.get(blob_sha)
    if cached is not None:
        return cached
    compile_misses += 1
    try:
        code = env.compile(source, name=name, filename=name)
        tmpl = Template.from_code(env, code, env.make_globals(None), None)
    except JinjaTemplateError as exc:  # syntax error reaching compile
        raise RenderError(str(exc), prompt_id=name, lineno=getattr(exc, "lineno", None))
    _COMPILED[blob_sha] = tmpl
    _COMPILE_ORDER.append(blob_sha)
    if len(_COMPILE_ORDER) > _CACHE_MAX:
        evict = _COMPILE_ORDER.pop(0)
        _COMPILED.pop(evict, None)
    return tmpl


def _stack_wrapped(base: Template, name: str) -> Template:
    """Return a cheap per-include view of ``base`` that maintains the render stack.

    The stack reflects the *active* include chain (pushed when the child actually
    renders, popped when it finishes) so diamonds are fine and only true cycles /
    excessive depth raise.
    """

    wrapper = object.__new__(type(base))
    wrapper.__dict__ = base.__dict__.copy()
    orig = base.root_render_func

    def rrf(context):
        ctx = _current.get()
        assert ctx is not None
        if name in ctx.stack:
            raise IncludeCycle(ctx.stack + [name])
        if len(ctx.stack) >= DEPTH_LIMIT:
            raise IncludeDepthExceeded(DEPTH_LIMIT, ctx.stack + [name])
        ctx.stack.append(name)
        try:
            yield from orig(context)
        finally:
            ctx.stack.pop()

    wrapper.root_render_func = rrf
    return wrapper


def precompile(blob_sha: str, source: str) -> None:
    """Warm the compiled-template cache for a blob (eager-warm at boot/commit)."""

    _compile(_ENV, blob_sha, source, blob_sha)


def render(
    snapshot: EnvSnapshot,
    prompt_id: str,
    flags: Mapping[str, Any],
    variables: Mapping[str, Any],
    content: ContentProvider,
    *,
    defaults: Mapping[str, Any] | None = None,
    pin: Mapping[str, tuple[int, str]] | None = None,
) -> RenderResult:
    """Resolve, compile, and render ``prompt_id``. Raises core errors on failure.

    ``defaults`` are DB-held values for optional variables, applied pre-render so
    ``StrictUndefined`` stays on. ``pin`` (prompt_id -> (version, commit)) replays
    an exact prior response, bypassing targeting for the pinned prompts (§9).
    """

    ctx = _RenderCtx(snapshot=snapshot, flags=flags, content=content, pin=pin or {})
    root = _pinned(ctx, prompt_id) or resolve(snapshot, prompt_id, flags, skips=ctx.skips)
    if root.content_fallback:
        ctx.fallback = True

    blob, root = _fetch(ctx, prompt_id, root)  # §10 within-version fallback
    ctx.contributions[prompt_id] = root
    return _render_compiled(ctx, prompt_id, blob.blob_sha, blob.source, variables, defaults, root)


def render_source(
    snapshot: EnvSnapshot,
    prompt_id: str,
    source: str,
    flags: Mapping[str, Any],
    variables: Mapping[str, Any],
    content: ContentProvider,
    *,
    defaults: Mapping[str, Any] | None = None,
) -> RenderResult:
    """Render an explicit top-level ``source`` (e.g. a draft), resolving its
    ``{% include %}`` targets through targeting. Used for draft/preview renders
    where the top template is not yet a committed SHA.
    """

    import hashlib

    ctx = _RenderCtx(snapshot=snapshot, flags=flags, content=content)
    root = Resolution(prompt_id, 0, "draft", "live", "default", None, None)
    ctx.contributions[prompt_id] = root
    blob_sha = "draft:" + hashlib.sha256(source.encode()).hexdigest()[:16]
    return _render_compiled(ctx, prompt_id, blob_sha, source, variables, defaults, root)


def _render_compiled(
    ctx: _RenderCtx,
    prompt_id: str,
    blob_sha: str,
    source: str,
    variables: Mapping[str, Any],
    defaults: Mapping[str, Any] | None,
    root: Resolution,
) -> RenderResult:
    base = _compile(_ENV, blob_sha, source, prompt_id)
    tmpl = _stack_wrapped(base, prompt_id)

    render_vars: dict[str, Any] = {}
    if defaults:
        render_vars.update(defaults)
    render_vars.update(variables)

    # Inject a lenient (base) Undefined for every closure-optional variable that
    # wasn't supplied, so guards (`{% if x %}`, `{% for m in x %}`) render while
    # any *required* missing variable still raises under StrictUndefined (→ 422).
    for name in _closure_optionals(ctx, prompt_id, source, blob_sha):
        if name not in render_vars:
            render_vars[name] = Undefined(name=name)

    token = _current.set(ctx)
    try:
        text = tmpl.render(render_vars)
    except UndefinedError as exc:
        raise MissingVariable(_undefined_name(exc), prompt_id) from exc
    except (IncludeCycle, IncludeDepthExceeded):
        raise
    except JinjaTemplateError as exc:
        raise RenderError(str(exc), prompt_id=prompt_id, lineno=getattr(exc, "lineno", None))
    finally:
        _current.reset(token)

    return RenderResult(
        text=text,
        root=root,
        contributions=ctx.contributions,
        content_fallback=ctx.fallback,
        skips=ctx.skips,
    )


def _undefined_name(exc: UndefinedError) -> str:
    # jinja2 message form: "'foo' is undefined"
    msg = str(exc)
    if msg.startswith("'") and "'" in msg[1:]:
        return msg[1:].split("'", 1)[0]
    return msg
