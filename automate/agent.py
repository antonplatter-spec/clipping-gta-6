"""Claude as the editor: the select loop (③), the dub translation (⑥) and the key test, on the Messages API with
tool use. The loop is hosted here because the tools read local files and run local clipper commands, and every
submission is validated before it is accepted.

    select_picks(job_id, *, max_picks, avoid_openers=(), log=None, cancel=None) -> Selection
    translate_dub(job_id, pick_idx, lang, segments_report, *, log=None, cancel=None) -> dict   (the --translation JSON)
    test_key(api_key=None) -> (ok, German message)
    build_system_prompt(profile) -> [system blocks] ; system_prompt_chars(profile) -> int
    build_select_request(job_row, *, max_picks, avoid_openers=()) -> (JobContext, request params)   (for --dry-run)
    make_client(api_key)     the SDK client; tests replace it with a scripted fake

Errors: MissingApiKey (no key; InvalidApiKey, a subclass, when Anthropic rejects the stored one), BudgetExceeded (the
daily limit, checked before every request), AgentError (.retryable, .code; str(e) is German for the dashboard;
AgentCanceled when `cancel` is set). The agent never writes the DB except usage rows; it keeps a copy of each
conversation (images elided) in data/tmp/agent/job-<id>/ for debugging, and a resumable checkpoint of an unfinished
selection there (select.resume.json, see _Resume): a selection interrupted by a server stop, a retryable API error,
a missing key or the daily limit continues after its last completed turn instead of paying for them again.

Request shape: model from claude.model, adaptive thinking (the only mode on Sonnet 5.5 / Opus 5.5; summarized, so
the saved conversations show Claude's reasoning) with effort from claude.effort, tool_choice auto (forced tool use
is a 400 on these models: the prompt steers, and a turn without a tool call gets a nudge), server-side refusal
fallback ("default"), streaming (so cancel works mid-generation). When the pipeline's gate rejected this source's
last selection (job params.check_feedback, a `clipper check` report), the first message lists those fails
(gate_feedback(report) -> [(where, fail)]) so Claude fixes them instead of repeating them.
Caching: tools + system form a 1-hour cached prefix shared by every job of a profile (the breakpoint is on the
research block); top-level automatic caching (5 minutes) carries the growing conversation within a job. Usage is
logged per request (per served model when a fallback ran) under purpose select | fix | dub_translate | test.

    python -m automate.agent --dry-run <job id or job folder> [--max-picks N] [--dump request.json]
prints the request (system block sizes, tools, first message outline, images) without sending it.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

import anthropic

from . import agent_tools as T
from . import costs, prompts, runtime, store

BETAS = ["server-side-fallback-2026-07-01"]
MAX_TOKENS = 32000  # thinking + the reply; a full submit_picks of several picks is ~5k
DUB_MAX_TOKENS = 16000
TEST_MAX_TOKENS = 64
MAX_NUDGES = 2  # turns without a tool call (or cut off at max_tokens) before giving up
MAX_PAUSES = 3
MAX_UPFRONT_SHEETS = 10
CANDIDATES_MD_CHARS = 60000
RESUME_MAX_AGE_S = 12 * 3600  # an interrupted selection older than this starts over
MODEL_LABELS = {"claude-sonnet-5-5": "Claude Sonnet 5.5", "claude-opus-5-5": "Claude Opus 5.5"}
# Thinking is always on for these models; "summarized" costs the same and makes the saved conversations readable.
THINKING = {"type": "adaptive", "display": "summarized"}
STOP_REASONS = ("end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn")
NUDGE = ("You ended your turn without calling a tool, and nothing is saved until submit_picks accepts it. Continue "
         "with the tools, and finish with submit_picks: your picks, or an empty list with no_clip_reason if nothing "
         "in this source is worth posting.")
CUT_OFF = ("Your last reply hit the output limit and was cut off; any tool call in it was not run. Continue more "
           "briefly: fewer words before a tool call, and no picks repeated in full except in submit_picks.")


class MissingApiKey(Exception):
    """No Anthropic key is stored: the job waits until one is saved."""


class BudgetExceeded(Exception):
    """Today's spend reached claude.daily_limit_eur: the job waits for the next day or a higher limit."""


class AgentError(Exception):
    """str(e) is German for the dashboard; `retryable` says whether trying again later can help."""

    def __init__(self, message, *, retryable=False, code="agent_error"):
        super().__init__(message)
        self.retryable, self.code = retryable, code


class InvalidApiKey(MissingApiKey, AgentError):
    """Anthropic rejected the stored key (401): like a missing key, the job waits for a new one."""

    def __init__(self, message):
        AgentError.__init__(self, message, retryable=False, code="invalid_key")


class AgentCanceled(AgentError):
    def __init__(self, message="Abgebrochen."):
        super().__init__(message, retryable=False, code="canceled")


@dataclass
class Selection:
    picks: list
    reasons: list
    click_triggers: list
    no_clip_reason: object
    check_report: dict
    turns: int
    usage: dict = field(default_factory=dict)


def make_client(api_key):
    """The SDK client (it retries 408/409/429/5xx and connection errors itself, twice, with backoff)."""
    return anthropic.Anthropic(api_key=api_key, max_retries=2)


def build_system_prompt(profile):
    return prompts.system_blocks(profile)


def system_prompt_chars(profile):
    return sum(len(b["text"]) for b in build_system_prompt(profile))


# ---------------------------------------------------------------- errors, budget, usage

def _api_key():
    key = (store.get_secret("anthropic_api_key") or "").strip()
    if not key:
        raise MissingApiKey("Kein Anthropic-API-Schlüssel gespeichert. Bitte unter „Kosten & Schlüssel“ "
                            "eintragen.")
    return key


def _settings():
    s = store.all_settings()
    return s["claude.model"], s["claude.effort"], int(s["claude.max_turns"]), int(s["claude.max_fix_rounds"])


def _label(model):
    return MODEL_LABELS.get(model, model)


def _cls(*names):
    return tuple(c for c in (getattr(anthropic, n, None) for n in names) if isinstance(c, type))


def _api_error(e, model):
    """An SDK exception as AgentError / InvalidApiKey with a German message (the SDK has already retried)."""
    status = getattr(e, "status_code", None)
    detail = getattr(e, "message", None) or str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return InvalidApiKey("Anthropic hat den API-Schlüssel abgelehnt (ungültig oder widerrufen). Bitte unter "
                             "„Kosten & Schlüssel“ einen gültigen Schlüssel eintragen "
                             "(console.anthropic.com → API Keys).")
    if isinstance(e, _cls("PaymentRequiredError", "ContractSpendLimitReachedError", "EnforcedSpendLimitReachedError")) \
            or isinstance(e, anthropic.BadRequestError) and "credit balance" in detail.lower():
        return AgentError("Das Anthropic-Konto hat kein Guthaben mehr oder sein Ausgabenlimit erreicht. Bitte unter "
                          "console.anthropic.com → Billing aufladen oder das Limit erhöhen.", code="billing")
    if isinstance(e, anthropic.PermissionDeniedError):
        return AgentError(f"Der API-Schlüssel darf {_label(model)} nicht verwenden (403). Bitte die Berechtigungen "
                          "des Schlüssels in der Anthropic-Konsole prüfen.", code="permission")
    if isinstance(e, anthropic.NotFoundError):
        return AgentError(f"Das Modell {_label(model)} ist für diesen Schlüssel nicht verfügbar (404). Unter "
                          "Einstellungen → Claude ein anderes Modell wählen.", code="model_not_found")
    if isinstance(e, anthropic.RateLimitError):
        return AgentError("Anthropic meldet zu viele Anfragen (429). Der Job wird später erneut versucht.",
                          retryable=True, code="rate_limited")
    if isinstance(e, anthropic.InternalServerError):
        return AgentError(f"Anthropic ist gerade überlastet oder gestört ({status}). Der Job wird später erneut "
                          "versucht.", retryable=True, code="server_error")
    if isinstance(e, _cls("RequestTooLargeError", "ContentTooLargeError")):
        return AgentError("Die Anfrage an Claude war zu groß (413).", code="too_large")
    if isinstance(e, anthropic.APIConnectionError):  # includes timeouts
        return AgentError("Keine Verbindung zu Anthropic (Netzwerk oder Zeitüberschreitung). Der Job wird später "
                          "erneut versucht.", retryable=True, code="connection")
    if isinstance(e, anthropic.APIStatusError):
        retry = status is not None and (status in (408, 409) or status >= 500)
        return AgentError(f"Anthropic hat die Anfrage abgelehnt ({status}): {detail[:300]}", retryable=retry,
                          code="api_error")
    return AgentError(f"Fehler bei der Anfrage an Claude: {detail[:300]}", retryable=True, code="api_error")


def _budget_message():
    limit = store.get_setting("claude.daily_limit_eur")
    return (f"Das Tageslimit für Claude ist erreicht ({costs.eur_text(costs.spent_today_eur())} von "
            f"{costs.eur_text(limit)}). "
            "Der Job läuft am nächsten Tag weiter oder sobald das Limit erhöht wird.")


def _type(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def record_usage(job_id, purpose, message, requested_model, request_id=None):
    """store.add_usage rows for a response: one per attempt when a fallback ran (usage.iterations is the per-attempt
    source of truth then), else one for the model that served it."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return []
    served = getattr(message, "model", None) or requested_model
    iters = [it for it in getattr(usage, "iterations", None) or [] if _type(it) in ("message", "fallback_message")]
    entries = [(getattr(it, "model", None) or served, it) for it in iters] or [(served, usage)]
    rows = []
    for model, u in entries:
        n = costs.normalize_usage(u)
        rows.append(store.add_usage(job_id, purpose, model, n["input"], n["output"],
                                    n["cache_write_5m"] + n["cache_write_1h"], n["cache_read"], request_id,
                                    cache_creation_1h_tokens=n["cache_write_1h"]))
    return rows


class _Session:
    """One job's requests: cancel and budget checks before each, streaming, error mapping, usage logging + totals."""

    def __init__(self, client, job_id, cancel=None):
        self.client, self.job_id, self.cancel = client, job_id, cancel
        self.rows, self.requests = [], 0

    def check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise AgentCanceled()

    def request(self, params, purpose):
        self.check_cancel()
        if not costs.budget_ok():
            raise BudgetExceeded(_budget_message())
        try:
            with self.client.beta.messages.stream(**params) as stream:
                for _event in stream:
                    if self.cancel is not None and self.cancel.is_set():
                        self._partial(stream, params["model"], purpose)
                        raise AgentCanceled()
                message = stream.get_final_message()
                request_id = getattr(stream, "request_id", None)
        except anthropic.APIError as e:
            raise _api_error(e, params["model"]) from e
        self.requests += 1
        self.rows += record_usage(self.job_id, purpose, message, params["model"], request_id)
        return message

    def _partial(self, stream, model, purpose):
        """A canceled stream still bills what it generated: log the snapshot's usage when there is one."""
        try:
            snap = stream.current_message_snapshot
            self.rows += record_usage(self.job_id, purpose, snap, model, getattr(stream, "request_id", None))
        except Exception:
            pass

    def totals(self):
        keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd", "cost_eur")
        out = {k: sum(r.get(k) or 0 for r in self.rows) for k in keys}
        out["cost_usd"], out["cost_eur"] = round(out["cost_usd"], 6), round(out["cost_eur"], 6)
        out["requests"] = self.requests
        return out


def _echo_content(content):
    """The assistant turn to send back: unchanged, except after a mid-output fallback, where the declined model's
    thinking and tool calls before the last `fallback` block are left out (the API's echo rule); its text stays."""
    content = list(content or [])
    marks = [i for i, b in enumerate(content) if _type(b) == "fallback"]
    if not marks:
        return content
    return [b for i, b in enumerate(content) if i >= marks[-1] or _type(b) in ("text", "fallback")]


def _refusal(message, what):
    details = getattr(message, "stop_details", None)
    cat = getattr(details, "category", None) if details is not None else None
    return AgentError(f"Claude hat {what} abgelehnt (Sicherheitsfilter{': ' + cat if cat else ''}).",
                      code="refusal")


# ---------------------------------------------------------------- the select request

def _time_base(ctx):
    dur = f"{ctx.duration:.0f} s of video" if ctx.duration else "unknown length"
    if ctx.offset:
        return f"{dur}; offset {ctx.offset:g} s (candidates.md timestamps = job seconds + {ctx.offset:g})"
    return f"{dur}; offset 0 (candidates.md timestamps are job seconds as m:ss)"


def _hooks_pref(value):
    if value == "auto":
        return "auto: simulate decides how many to keep (its keep line shows the count)"
    return f"{value}: simulate's keep line keeps up to {value} hooks, each a different type"


VOICE_TEXT = {"off": "off: no voiceover on any video", "all": "all: every video gets a voiceover",
              "auto": "auto: add one only where the research says it helps"}
TIER_ORDER = {"strong": 0, "maybe": 2, "skip": 3}


def _sheet_order(cands, scens):
    """Contact sheets in the order they're worth attaching: strong, scenarios, maybe, skip (by score within a tier)."""
    rows = [(TIER_ORDER.get(c.get("tier"), 3), -(c.get("score") or 0), f"#{c['id']}", c) for c in cands]
    rows += [(1, 0, str(s.get("id")), s) for s in scens]
    return [r[3] for r in sorted(rows, key=lambda r: r[:3])]


def _sheet_name(c):
    return re.sub(r"^review/|\.jpg$", "", str(c.get("contact_sheet") or ""))


def _span(c):
    return f"{float(c['start']):.1f}-{float(c['end']):.1f} s ({float(c['end']) - float(c['start']):.0f} s)"


FAIL_KEYS = ("variety", "length", "labels", "footage", "fx")  # clipper.hooks.has_fails: these keys are fails


def gate_feedback(report):
    """[(where, fail)] from a `clipper check` report ({"video N": entry}) the pipeline's gate stored as
    params.check_feedback after rejecting this source's last selection."""
    out = []
    for video, entry in (report or {}).items() if isinstance(report, dict) else ():
        if not isinstance(entry, dict):
            continue
        for kind in ("hooks", "titles"):
            for text, v in (entry.get(kind) or {}).items():
                for fail in (v or {}).get("fails") or []:
                    out.append((f"{video}, {kind[:-1]} \"{text}\"", fail))
        for key in FAIL_KEYS:
            if entry.get(key):
                v = entry[key]
                out.append((f"{video}, {key}", "; ".join(map(str, v)) if isinstance(v, list) else str(v)))
    return out


def _first_message(ctx, max_turns, max_fix):
    data = T.candidates(ctx)
    cands, scens = data.get("candidates") or [], data.get("scenarios") or []
    sheets = []
    for c in _sheet_order(cands, scens):
        name = _sheet_name(c)
        f = ctx.dir / "review" / f"{name}.jpg"
        if len(sheets) >= MAX_UPFRONT_SHEETS or not name or not f.exists():
            continue
        try:
            block, _ = T.sheet_image(f)
        except Exception:  # a broken sheet: the model can still read the transcript
            continue
        what = f"candidate #{c['id']}" if "tier" in c else f"scenario {c.get('id')}"
        sheets.append((f"Contact sheet {name} · {what} · {_span(c)}", block))
        ctx.sheets_attached.add(name)
    lines = [f"recommended videos: {data.get('recommended', '?')} · content: {data.get('profile', '?')} · suggested "
             f"clip type: {data.get('clip_type', '?')}"]
    for c in cands:
        sig = c.get("signals") or {}
        name = _sheet_name(c)
        bits = [f"#{c['id']} {c.get('tier', '?')}", _span(c), f"score {c.get('score')}"]
        if sig.get("jump_lu") is not None:
            bits.append(f"jump +{sig['jump_lu']} LU")
        if sig.get("most_replayed") is not None:
            bits.append(f"replayed {sig['most_replayed']}")
        if sig.get("hype_hits"):
            bits.append("hype: " + ", ".join(map(str, sig["hype_hits"][:5])))
        bits.append(f"opens with {c.get('opens_with', '?')}")
        bits.append(f"sheet {name} ({'attached' if name in ctx.sheets_attached else 'view_frame sheet=' + name})")
        lines.append(" · ".join(bits))
    for s in scens:
        name = _sheet_name(s)
        lines.append(f"{s.get('id')} scenario · {_span(s)} · cue \"{s.get('cue')}\" · sheet {name} "
                     f"({'attached' if name in ctx.sheets_attached else 'view_frame sheet=' + name})")
    md = (ctx.dir / "candidates.md").read_text(encoding="utf-8")
    if len(md) > CANDIDATES_MD_CHARS:
        md = md[:CANDIDATES_MD_CHARS] + "\n[... candidates.md cut here; the index above lists every candidate]"
    meta, job = ctx.meta, ctx.job
    live = job.get("kind") == "live_chunk" or bool(meta.get("live"))
    recorded = None
    if live and meta.get("recorded_at"):
        recorded = datetime.fromtimestamp(float(meta["recorded_at"])).strftime("%Y-%m-%d %H:%M")
    openers = list(dict.fromkeys(x.strip() for x in ctx.avoid_openers))
    music = T.music_moods(ctx)
    if music is None:
        music_text = "unknown (the music library couldn't be read): prefer \"music\": [] unless a mood clearly adds"
    else:
        moods, hits = music
        music_text = (f"moods with an approved track: {', '.join(moods)}" if moods else
                      "no approved track in any mood, so music windows would be skipped: use \"music\": [] where check "
                      "notes a quiet stretch") + (f"; hit sounds: {', '.join(hits)}" if hits else "; no hit sounds")
    facts = [
        ("Job", f"{ctx.clipper_job} (worker job #{job.get('id')}, "
                f"{'a chunk of a live stream' if live else 'an uploaded video'})"),
        ("Source", f"\"{meta.get('title') or job.get('title') or '?'}\" by "
                   f"{meta.get('channel') or job.get('channel') or '?'} · "
                   f"{meta.get('url') or job.get('source_url') or '?'}"),
        ("Recorded", f"{recorded} (other streams' chunks from around then can be searched with find_line)"
         if recorded else None),
        ("Video", _time_base(ctx)),
        ("Profile", ctx.profile),
        ("Videos you may make from this source", f"at most {ctx.max_picks} (the analysis recommends "
                                                 f"{data.get('recommended', '?')})"),
        ("Hook variants per video", _hooks_pref(ctx.hooks_per_video)),
        ("Voiceover", VOICE_TEXT.get(ctx.voice, ctx.voice)),
        ("How earlier videos' hooks and titles open", "; ".join(f"\"{o}\"" for o in openers) + " (don't start a "
         "hook or title with the same three words as any of these)" if openers else "no earlier videos yet"),
        ("Music", music_text),
        ("Requests", f"{max_turns} in total for this job (every round trip with tool results is one)"),
        ("Fix rounds", f"{max_fix} rejected submission(s) come back for fixing; the next rejection fails the job"
         if max_fix else "none: the first rejected submission fails the job"),
    ]
    feedback = gate_feedback((job.get("params") or {}).get("check_feedback"))
    return {"role": "user", "content": prompts.select_message(facts, "\n".join(lines), md, sheets, feedback)}


def build_select_request(job, *, max_picks, avoid_openers=(), cancel=None, log=None):
    """(JobContext, request params) exactly as select_picks sends its first request. Raises AgentError when the job
    has nothing to select from."""
    model, effort, max_turns, max_fix = _settings()
    try:
        ctx = T.JobContext.load(job, max_picks=max_picks, avoid_openers=avoid_openers, cancel=cancel, log=log)
        first = _first_message(ctx, max_turns, max_fix)
    except LookupError as e:
        raise AgentError(str(e), code="no_analysis") from None
    except T.ToolCanceled:
        raise AgentCanceled() from None
    params = {"model": model, "max_tokens": MAX_TOKENS, "system": build_system_prompt(job["profile"]),
              "tools": T.TOOLS, "tool_choice": {"type": "auto"}, "thinking": THINKING,
              "output_config": {"effort": effort}, "cache_control": {"type": "ephemeral"}, "betas": BETAS,
              "fallbacks": "default", "messages": [first]}
    return ctx, params


def _say_call(ctx, name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == "grep_transcript":
        what = (f"„{inp['pattern']}“" if inp.get("pattern") else
                f"um {inp['around_s']:.0f} s" if T._num(inp.get("around_s")) else
                f"{inp.get('from_s', 0)}–{inp.get('to_s', '…')} s")
        ctx.say(f"Claude liest das Transkript ({what})")
    elif name == "view_frame":
        what = f"den Kontaktbogen {inp['sheet']}" if inp.get("sheet") else \
            f"{'ein Raster-Bild' if inp.get('grid') else 'ein Bild'} bei {inp.get('seconds')} s"
        ctx.say(f"Claude sieht sich {what} an")
    elif name == "find_line":
        ctx.say(f"Claude sucht „{inp.get('pattern')}“ in anderen Streams")
    elif name == "check":
        ctx.say("Claude prüft einen Entwurf")
    elif name == "simulate":
        ctx.say("Claude simuliert die Hooks …")
    elif name == "submit_picks":
        n = len(inp.get("picks") or []) if isinstance(inp.get("picks"), list) else 0
        ctx.say(f"Claude gibt {n} Pick(s) ab" if n else "Claude gibt keinen Clip ab")


def _tool_result(block, res):
    out = {"type": "tool_result", "tool_use_id": block.id, "content": res.content}
    if res.is_error:
        out["is_error"] = True
    return out


def select_picks(job_id, *, max_picks, avoid_openers=(), log=None, cancel=None):
    """Claude picks the moments of an analyzed source or live chunk. Returns a Selection whose picks are exactly what
    `clipper render` accepts and pass `clipper check --job` without fails (or none, with no_clip_reason)."""
    job = store.get_job(job_id)
    if not job:
        raise AgentError(f"Job {job_id} gibt es nicht.", code="no_job")
    if int(max_picks) < 1:
        raise ValueError("max_picks must be at least 1")
    key = _api_key()
    if not costs.budget_ok():
        raise BudgetExceeded(_budget_message())
    model, _, max_turns, max_fix = _settings()
    ctx, params = build_select_request(job, max_picks=max_picks, avoid_openers=avoid_openers, cancel=cancel, log=log)
    sess = _Session(make_client(key), job_id, cancel)
    messages = params["messages"]
    turns = nudges = pauses = rejections = 0
    outcome, keep = "unfinished", False
    resume = _Resume(ctx, model, messages[0])
    state = resume.load()
    if state:
        messages[:] = state["messages"]
        turns, nudges, pauses, rejections = (int(state[k]) for k in ("turns", "nudges", "pauses", "rejections"))
        ctx.views, ctx.sheets_attached = int(state.get("views") or 0), set(state.get("sheets") or ())
        ctx.say(f"Setzt die unterbrochene Auswahl nach {turns} Anfrage(n) fort (bereits bezahlt, nicht wiederholt)")
    try:
        while turns < max_turns:
            if turns:
                resume.save(messages, turns=turns, nudges=nudges, pauses=pauses, rejections=rejections,
                            views=ctx.views, sheets=sorted(ctx.sheets_attached))
            ctx.say(f"Claude ({_label(model)}) denkt nach … (Anfrage {turns + 1} von {max_turns})")
            message = sess.request(params, "fix" if rejections else "select")
            turns += 1
            content = _echo_content(message.content)
            if message.stop_reason == "refusal":
                raise _refusal(message, "die Auswahl")
            if message.stop_reason not in STOP_REASONS:
                raise AgentError(f"Claudes Antwort endete unerwartet ({message.stop_reason}).", code="stop_reason")
            if content:  # an empty turn can't be sent back; the nudge below follows the last user turn instead
                messages.append({"role": "assistant", "content": content})
            if message.stop_reason == "pause_turn":
                pauses += 1
                if pauses > MAX_PAUSES:
                    raise AgentError("Claude hat die Antwort zu oft pausiert.", code="paused")
                continue
            calls = [b for b in content if _type(b) == "tool_use"]
            if message.stop_reason == "max_tokens" or not calls:
                nudges += 1
                if nudges > MAX_NUDGES:
                    raise AgentError("Claude hat mehrmals geantwortet, ohne eine Auswahl abzugeben"
                                     + (" (Ausgabelimit erreicht)." if message.stop_reason == "max_tokens" else "."),
                                     code="no_submission")
                cut = message.stop_reason == "max_tokens"
                results = [_tool_result(b, T.ToolResult(CUT_OFF, is_error=True)) for b in calls]
                messages.append({"role": "user", "content": results + [{"type": "text", "text": CUT_OFF if cut
                                                                         else NUDGE}]})
                continue
            results, submitted = [], False
            for block in calls:
                name = T.resolve_name(block.name) or block.name
                _say_call(ctx, name, block.input)
                if name != "submit_picks":
                    try:
                        res = T.run_tool(ctx, name, block.input)
                    except T.ToolCanceled:
                        raise
                    except Exception as e:  # a bug in a tool must not end the job: Claude can work around it
                        ctx.say(f"Werkzeugfehler in {name}: {e}")
                        res = T.ToolResult(f"internal error in {name}: {e}", is_error=True)
                    results.append(_tool_result(block, res))
                    continue
                if submitted:
                    results.append(_tool_result(block, T.ToolResult("Only one submit_picks per turn; this one was "
                                                                     "ignored.", is_error=True)))
                    continue
                submitted = True
                out = T.submit(ctx, block.input if isinstance(block.input, dict) else {}, round_no=rejections + 1,
                               max_rounds=max_fix)
                if isinstance(out, T.Submission):
                    outcome = "accepted"
                    usage = sess.totals()
                    ctx.say(f"Auswahl angenommen: {len(out.picks)} Pick(s) nach {turns} Anfragen "
                            f"({costs.eur_text(usage['cost_eur'])})" if out.picks else
                            f"Kein Clip aus dieser Quelle: {out.no_clip_reason}")
                    return Selection(picks=out.picks, reasons=out.reasons, click_triggers=out.click_triggers,
                                     no_clip_reason=out.no_clip_reason, check_report=out.check_report, turns=turns,
                                     usage=usage)
                rejections += 1
                if rejections > max_fix:
                    outcome = "rejected"
                    first = out.content.split("\n", 2)[1:2]
                    why = ": " + first[0].lstrip("- ")[:300] if first else "."
                    raise AgentError(f"Claudes Picks bestehen die Prüfung auch nach {max_fix} Korrekturrunde(n) "
                                     f"nicht{why}", code="check_failed")
                ctx.say(f"Prüfung nicht bestanden – zurück an Claude (Korrekturrunde {rejections} von {max_fix})")
                results.append(_tool_result(block, out))
            left = max_turns - turns
            if left in (3, 1):
                results.append({"type": "text", "text": (
                    f"Note from the worker: {left} request{'s are' if left > 1 else ' is'} left for this job. "
                    + ("Finish with submit_picks: the picks you are sure of, or an empty list with no_clip_reason."
                       if left > 1 else "Call submit_picks now."))})
            messages.append({"role": "user", "content": results})
        outcome = "max_turns"
        raise AgentError(f"Claude hat nach {max_turns} Anfragen keine gültige Auswahl abgegeben (Limit „Höchstens "
                         "Schritte pro Auswahl“).", code="max_turns")
    except T.ToolCanceled:
        keep = True
        raise AgentCanceled() from None
    except (BudgetExceeded, MissingApiKey, AgentCanceled):  # the job waits or stops and comes back to this selection
        keep = True
        raise
    except AgentError as e:
        keep = e.retryable
        raise
    finally:
        if not keep:
            resume.clear()
        _save_conversation(ctx, params, outcome, sess)


class _Resume:
    """The select conversation after each completed turn, so a job interrupted mid-selection (server stopped, a
    429 retried later, the budget spent) continues where it was instead of paying for the same turns again. Valid
    only for the same model and the same first message (same analysis, max_picks, openers, gate feedback) and for
    RESUME_MAX_AGE_S; images and thinking signatures are kept (the turns go back to the API unchanged)."""

    def __init__(self, ctx, model, first):
        self.path = ctx.scratch / "select.resume.json"
        self.model = model
        self.key = hashlib.sha256(json.dumps(_raw(first), sort_keys=True).encode("utf-8")).hexdigest()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("key") != self.key or data.get("model") != self.model \
                or time.time() - float(data.get("saved_at") or 0) > RESUME_MAX_AGE_S \
                or not isinstance(data.get("messages"), list) or len(data["messages"]) < 2:
            return None
        return data

    def save(self, messages, **counters):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(json.dumps({"key": self.key, "model": self.model, "saved_at": time.time(),
                                       "messages": _raw(messages), **counters}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except (OSError, TypeError, ValueError):  # a missing checkpoint only costs a fresh start
            pass

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _raw(obj):
    """Plain JSON of request messages with SDK blocks, dumped exactly as the SDK sends them (nothing elided)."""
    if isinstance(obj, dict):
        return {k: _raw(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_raw(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if callable(getattr(obj, "model_dump", None)):
        return _raw(obj.model_dump(exclude_unset=True, mode="json", by_alias=True,
                                   exclude=getattr(obj, "__api_exclude__", None)))
    return _raw({k: v for k, v in vars(obj).items() if not k.startswith("_")})


# ---------------------------------------------------------------- debugging copies of conversations

def _jsonable(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "data" and isinstance(v, str) and len(v) > 200:
                out[k] = f"<{len(v)} base64 chars>"
            elif k == "signature" and isinstance(v, str):
                out[k] = "<signature>"
            else:
                out[k] = _jsonable(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    for attr in ("to_dict", "model_dump"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _jsonable(fn())
            except Exception:
                pass
    if hasattr(obj, "__dict__"):
        return _jsonable({k: v for k, v in vars(obj).items() if not k.startswith("_")})
    return str(obj)


def _save_conversation(ctx, params, outcome, sess=None, name="select"):
    try:
        ctx.scratch.mkdir(parents=True, exist_ok=True)
        data = {"outcome": outcome, "saved_at": time.time(), "model": params.get("model"),
                "usage": sess.totals() if sess else None, "messages": _jsonable(params.get("messages"))}
        (ctx.scratch / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- dub translation

CUE = re.compile(r"\[[^\]]*\]")


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").replace("*", "")).strip().lower()


def dub_errors(tr, segments, english):
    """What's wrong with a translation before clipper's validator sees it: one line per segment with spoken words,
    the source's [cues] kept, >= 3 hooks paired with titles, nothing left in English."""
    errs = []
    lines = tr.get("lines")
    if not (isinstance(lines, list) and len(lines) == len(segments)):
        errs.append(f"lines: need exactly {len(segments)} (one per segment, in order), got "
                    f"{len(lines) if isinstance(lines, list) else 0}")
    else:
        for i, (line, seg) in enumerate(zip(lines, segments)):
            if not isinstance(line, str) or not CUE.sub(" ", line).strip():
                errs.append(f"lines[{i}]: needs spoken words, not only [cues]")
                continue
            missing = [c for c in CUE.findall(str(seg.get("text") or "")) if c not in line]
            if missing:
                errs.append(f"lines[{i}]: keep the source's delivery cue(s) {missing} unchanged")
    hooks, titles = tr.get("hooks"), tr.get("titles")
    if not (isinstance(hooks, list) and len(hooks) >= 3 and all(isinstance(h, str) and h.strip() for h in hooks)):
        errs.append("hooks: need at least 3 non-empty hooks")
    elif not (isinstance(titles, list) and len(titles) == len(hooks) and all(isinstance(t, str) and t.strip()
                                                                             for t in titles)):
        errs.append(f"titles: need one per hook ({len(hooks)}), same order")
    elif any(len(t) > 100 for t in titles):
        errs.append("titles: at most 100 characters each")
    if not (isinstance(tr.get("description"), str) and tr["description"].strip()):
        errs.append("description: one sentence is required")
    tags = tr.get("hashtags")
    if not (isinstance(tags, list) and all(isinstance(t, str) and re.fullmatch(r"#\w+", t) for t in tags)):
        errs.append("hashtags: a list of tags like \"#gta6\"")
    eng = {_norm(x) for k in ("hooks", "titles") for x in english.get(k) or []} | {_norm(english.get("description"))}
    eng.discard("")
    for kind in ("hooks", "titles"):
        for t in tr.get(kind) or []:
            if _norm(t) in eng:
                errs.append(f"{kind}: {t!r} is still the English text")
    if _norm(tr.get("description")) in eng:
        errs.append("description: still the English text")
    return errs


def _clipper_job_for(job):
    """The clipper job whose picks.json holds the pick being dubbed: the job's own, the dubbed video's source job, or
    the parent's."""
    if job.get("clipper_job"):
        return job["clipper_job"], job["profile"]
    vid = (job.get("params") or {}).get("video_id")
    video = store.get_video(vid) if vid else None
    for jid in ((video or {}).get("job_id"), job.get("parent_id")):
        src = store.get_job(jid) if jid else None
        if src and src.get("clipper_job"):
            return src["clipper_job"], src["profile"]
    return None, job["profile"]


def _validator_errors(job, pick_idx, lang, tr, n_lines, cancel, log):
    """clipper.dub.validate_translation (through the bridge) is the judge; [] when it can't run (the dub render
    checks again before any audio work)."""
    cj, profile = _clipper_job_for(job)
    if not cj:
        return []
    try:
        out = runtime.run_bridge("dub_validate", {"job": str(T.job_dir(cj)), "pick": int(pick_idx), "lang": lang,
                                                  "translation": tr, "n_lines": n_lines}, profile, timeout=120,
                                 cancel=cancel)
    except runtime.BridgeError as e:
        if e.canceled:
            raise AgentCanceled() from None
        if log:
            log(f"Übersetzungsprüfung nicht möglich: {e.message}")
        return []
    except ValueError:
        return []
    return [str(x) for x in out.get("errors") or []]


def _parse_translation(message, lang):
    text = "".join(getattr(b, "text", "") for b in message.content or [] if _type(b) == "text").strip()
    if message.stop_reason == "max_tokens":
        return None, ["the reply was cut off at the output limit: keep the lines short"]
    try:
        data = json.loads(text)
    except ValueError:
        return None, ["the reply must be the JSON object only"]
    if not isinstance(data, dict):
        return None, ["the reply must be a JSON object"]
    keys = ("lines", "hooks", "titles", "description", "hashtags", "credit", "disclosure")
    return {"lang": lang, **{k: data[k] for k in keys if k in data}}, []


def translate_dub(job_id, pick_idx, lang, segments_report, *, log=None, cancel=None):
    """The translation JSON `clipper dub render --translation` accepts, for pick `pick_idx` (1-based) of the job's
    picks.json. One request with a JSON-schema output; validated (locally, then by clipper's validator) and retried
    once with the validator's message."""
    job = store.get_job(job_id)
    if not job:
        raise AgentError(f"Job {job_id} gibt es nicht.", code="no_job")
    segs = (segments_report or {}).get("segments")
    if not isinstance(segs, list) or not segs:
        raise AgentError("Der Clip hat keine gesprochenen Zeilen, die übersetzt werden könnten.", code="no_lines")
    key = _api_key()
    model, effort, _, _ = _settings()
    sess = _Session(make_client(key), job_id, cancel)
    messages = [{"role": "user", "content": prompts.dub_message(lang, segments_report)}]
    params = {"model": model, "max_tokens": DUB_MAX_TOKENS, "system": prompts.dub_system(lang),
              "thinking": THINKING,
              "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": prompts.DUB_SCHEMA}},
              "betas": BETAS, "fallbacks": "default", "messages": messages}
    english = segments_report.get("english") or {}
    errs = []
    for attempt in (1, 2):
        if log:
            log(f"Claude übersetzt den Clip ins {prompts.language_name(lang)}e …" if attempt == 1 else
                "Claude bessert die Übersetzung nach …")
        message = sess.request(params, "dub_translate")
        if message.stop_reason == "refusal":
            raise _refusal(message, "die Übersetzung")
        tr, errs = _parse_translation(message, lang)
        if not errs:
            errs = dub_errors(tr, segs, english) or _validator_errors(job, pick_idx, lang, tr, len(segs), cancel, log)
        if not errs:
            return tr
        messages += [{"role": "assistant", "content": _echo_content(message.content)},
                     {"role": "user", "content": "The validator rejected the translation:\n- " + "\n- ".join(errs)
                      + "\nReturn the corrected JSON object."}]
    raise AgentError("Claudes Übersetzung ist auch nach einer Korrektur ungültig: " + "; ".join(errs)[:400],
                     code="dub_invalid")


# ---------------------------------------------------------------- key test

def test_key(api_key=None):
    """(ok, German message). The cheapest real call: a 64-token message (checks the key, the model and the account's
    credit; logged as purpose "test"); once today's budget is spent only the free model lookup."""
    key = (store.get_secret("anthropic_api_key") if api_key is None else api_key) or ""
    key = key.strip()
    if not key:
        return False, "Kein Anthropic-API-Schlüssel gespeichert."
    model = store.get_setting("claude.model")
    client = make_client(key)
    try:
        if costs.budget_ok():
            message = client.messages.create(model=model, max_tokens=TEST_MAX_TOKENS, output_config={"effort": "low"},
                                             messages=[{"role": "user", "content": "Antworte nur mit OK."}])
            record_usage(None, "test", message, model, getattr(message, "_request_id", None))
            return True, f"Der Schlüssel funktioniert: {_label(model)} hat geantwortet."
        client.models.retrieve(model)
        return True, (f"Der Schlüssel ist gültig und {_label(model)} verfügbar (ohne Testanfrage geprüft, weil das "
                      "Tageslimit erreicht ist).")
    except anthropic.APIError as e:
        err = _api_error(e, model)
        if err.code == "rate_limited":
            return True, "Der Schlüssel ist gültig, aber Anthropic meldet gerade zu viele Anfragen (429)."
        return False, str(err)


# ---------------------------------------------------------------- --dry-run

def _dry_job(ref, profile=None):
    if str(ref).isdigit() and store.get_job(int(ref)):
        return store.get_job(int(ref))
    d = T.job_dir(ref)
    if not d.is_dir():
        raise SystemExit(f"no DB job {ref} and no job folder {d} (set AUTOMATE_JOBS_DIR for another jobs folder)")
    meta = T._read_json(d / "job.json") or {}
    return {"id": 0, "kind": "live_chunk" if meta.get("live") else "source",
            "profile": profile or meta.get("profile") or store.get_setting("ui.default_profile"), "clipper_job": d.name,
            "title": meta.get("title"), "channel": meta.get("channel"), "source_url": meta.get("url")}


def _tokens(chars):
    return round(chars / 4)


def describe_request(params, ctx=None):
    """A readable outline of a request: what is cached, how big each part is, what the first message carries."""
    out = [f"model {params['model']} · max_tokens {params['max_tokens']} · thinking {params.get('thinking')} · "
           f"output_config {params.get('output_config')} · tool_choice {params.get('tool_choice')} · betas "
           f"{params.get('betas')} · fallbacks {params.get('fallbacks')!r} · automatic cache_control "
           f"{params.get('cache_control')}"]
    tools_json = json.dumps(params.get("tools") or [], ensure_ascii=False)
    out.append(f"tools ({len(params.get('tools') or [])}, {len(tools_json)} chars ≈ {_tokens(len(tools_json))} "
               "tokens):")
    for t in params.get("tools") or []:
        out.append(f"  {t['name']}{' (strict)' if t.get('strict') else ''}: description {len(t['description'])} "
                   f"chars, schema {len(json.dumps(t['input_schema']))} chars")
    out.append("system blocks:")
    for i, b in enumerate(params.get("system") or [], 1):
        out.append(f"  {i}. {len(b['text'])} chars ≈ {_tokens(len(b['text']))} tokens"
                   + (f" · cache_control {b['cache_control']}" if b.get("cache_control") else "")
                   + f" · starts \"{b['text'][:60]}\"")
    prefix = json.dumps([params.get("tools"), params.get("system")], ensure_ascii=False, sort_keys=False)
    out.append(f"cached prefix (tools + system): {len(prefix)} chars ≈ {_tokens(len(prefix))} tokens · sha256 "
               f"{hashlib.sha256(prefix.encode()).hexdigest()[:16]}")
    images, text_chars = 0, 0
    msgs = params.get("messages") or []
    for m in msgs:
        out.append(f"message ({m['role']}):")
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        for b in content:
            if _type(b) == "image":
                images += 1
                size = len(b["source"]["data"]) * 3 // 4
                out.append(f"  image {images}: {size // 1024} KB jpeg")
            else:
                text = b.get("text", "")
                text_chars += len(text)
                out.append(f"  text {len(text)} chars: {text.splitlines()[0][:80] if text else ''}")
    image_tokens = sum(_image_tokens(b) for m in msgs if isinstance(m["content"], list) for b in m["content"]
                       if _type(b) == "image")
    out.append(f"first message: {images} image(s) ≈ {image_tokens} tokens, {text_chars} text chars ≈ "
               f"{_tokens(text_chars)} tokens")
    if ctx is not None:
        out.append(f"job {ctx.clipper_job}: {ctx.duration} s, offset {ctx.offset}, voice {ctx.voice}, hooks "
                   f"{ctx.hooks_per_video}, max_picks {ctx.max_picks}, sheets attached {sorted(ctx.sheets_attached)}")
    return "\n".join(out)


def _image_tokens(block):
    """Claude's image cost: about width x height / 750 tokens."""
    import base64
    import io
    from PIL import Image
    try:
        w, h = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))).size
        return round(w * h / 750)
    except Exception:
        return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m automate.agent",
                                description="Build Claude's select request for a job and print it, without sending.")
    p.add_argument("--dry-run", metavar="JOB", required=True,
                   help="a DB job id, or a job folder name in the jobs dir ($AUTOMATE_JOBS_DIR or ./jobs)")
    p.add_argument("--profile", help="profile for a job folder (default: job.json's, else the dashboard default)")
    p.add_argument("--max-picks", type=int, default=3)
    p.add_argument("--avoid", action="append", default=[], help="an opener earlier videos use (repeatable)")
    p.add_argument("--dump", help="also write the whole request as JSON (images elided) to this file")
    args = p.parse_args(argv)
    job = _dry_job(args.dry_run, args.profile)
    ctx, params = build_select_request(job, max_picks=args.max_picks, avoid_openers=args.avoid)
    again = json.dumps([build_system_prompt(job["profile"]), T.TOOLS], ensure_ascii=False)
    stable = again == json.dumps([params["system"], params["tools"]], ensure_ascii=False)
    print(describe_request(params, ctx))
    print(f"prefix byte-stable across builds: {'yes' if stable else 'NO'}")
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(_jsonable({k: v for k, v in params.items()}), f, ensure_ascii=False, indent=1)
        print(f"request written to {args.dump}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
