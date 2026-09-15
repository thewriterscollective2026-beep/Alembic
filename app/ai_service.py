"""Claude-powered categorization, research, and newsletter writing.

The repurpose + "no-AI-writing" guidance below is embedded as prompt text (see
REPURPOSE_GUIDANCE / NO_AI_GUIDANCE) because Claude Code *skills* don't run
inside a deployed web app - edit those constants directly to change voice/style.
"""
import json
import logging
import anthropic
import re

from . import config
from .database import SessionLocal
from .email_template import THEMES, DEFAULT_THEME
from .models import UsageLog

_client = None
_usage_log = logging.getLogger("ai_service.usage")


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": config.WEB_SEARCH_MAX_USES,
}


def _complete(prompt: str, max_tokens: int = 2000, system: str | None = None,
              tools: list | None = None, model: str | None = None,
              effort: str | None = None, tag: str = "", description: str = "",
              cache_system: bool = False) -> str:
    """Call Claude and return the joined visible text. Every call is tagged, described,
    and its token usage recorded - both to the `ai_service.usage` logger (for tailing
    the console during dev) and to the `usage_logs` DB table (for the /dev/usage page,
    which is the only real spend visibility this app has - there's no Admin API key).

    cache_system=True marks the system prompt as a 1-hour prompt cache breakpoint -
    for calls that reuse the exact same system prompt across a span of minutes/hours
    (e.g. revise_newsletter across a back-and-forth review thread, where the gap
    between calls is however long the reviewer takes to reply - the 1h TTL survives
    that; the default 5-min TTL usually wouldn't). Cached reads cost ~10% of normal
    input price; a cache miss still costs the same as an uncached call plus a small
    write premium, so this is safe to enable even on a first, never-reused call."""
    kwargs = {}
    if tools:
        kwargs["tools"] = tools
    if effort:
        kwargs["output_config"] = {"effort": effort}
    system_text = system or "You are a precise, helpful assistant."
    if cache_system:
        system_param = [{
            "type": "text", "text": system_text,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]
    else:
        system_param = system_text
    msg = _get_client().messages.create(
        model=model or config.CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system_param,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    u = msg.usage
    input_tokens, output_tokens = u.input_tokens, u.output_tokens
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    _usage_log.info(
        "tag=%s model=%s input=%d output=%d cache_read=%d cache_write=%d",
        tag or "untagged", msg.model, input_tokens, output_tokens, cache_read, cache_write,
    )
    _record_usage(tag, description, msg.model, input_tokens, output_tokens, cache_read, cache_write)
    # Server tool blocks (e.g. web_search_tool_result) are skipped here on purpose -
    # only the model's own writing ends up in the joined text.
    return "".join(block.text for block in msg.content if block.type == "text").strip()


def _record_usage(tag, description, model, input_tokens, output_tokens, cache_read, cache_write):
    """Best-effort DB write for the usage log - must never break the actual call
    it's recording, so any DB problem here is swallowed, not raised."""
    db = None
    try:
        db = SessionLocal()
        db.add(UsageLog(
            tag=tag or "untagged", description=description, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        _usage_log.exception("failed to write usage log row")
    finally:
        if db is not None:
            db.close()


# Distilled from the user's content-repurposing skill (email-format adaptation).
REPURPOSE_GUIDANCE = """
ADAPT-BY-FORMAT, never mass-blast or copy. Turn the source newsletters INTO one
original email that stands on its own: a reader who never saw the sources should
get full value. Email-format rules:
- Subject line + opening line carry most of the engagement; make them specific.
- Synthesize across ALL sources; group related items into clear themes; lead with
  the most important or most interesting story.
- Every section stands alone and says WHY it matters, not just what happened.
- One clear primary call-to-action; don't dilute with competing CTAs.
- Keep the genuinely useful links; attribute when a point is drawn from a source.
- Preserve the sender's distinctive point of view and voice throughout.
- Never duplicate source sentences. Write it fresh as the sender's own curation.
"""

# Distilled from the user's avoid-ai-writing skill (concrete AI-isms to remove).
NO_AI_GUIDANCE = """
Remove AI writing tells. Hard rules:
- NO em dashes (the long dash or --). Use commas, periods, parentheses, or split
  the sentence. (A list item like "Term: description" is fine; don't use dashes.)
- No bold overuse: at most one bold phrase per section.
- No emoji inside headings.
- Never use "It's not X, it's Y" or "This isn't about X, it's Y" (including the
  two-sentence split form). State the positive point directly.
- Cut hollow intensifiers: genuine(ly), truly, really, "to be honest", "it's worth
  noting", "at its core", and "actually" used only for emphasis.
- Cut hedging: perhaps, could potentially, "it's important to note".
- Replace these AI-marker words on sight: delve, landscape/realm/paradigm/tapestry,
  embark, "testament to", robust, comprehensive, cutting-edge, leverage (use "use"),
  pivotal, underscores, meticulous, seamless, game-changer, "deep dive"/"dive into",
  unpack, intricate, ever-evolving, holistic, actionable, impactful, learnings,
  synergy, "in order to" (use "to"), utilize (use "use"), boasts, showcasing.
- Avoid the compulsive rule-of-three (adjective, adjective, and adjective).
- Each paragraph bridges to the last. Prefer concrete specifics over vague praise.
- Vary sentence length for a natural human rhythm. Cut filler. Sound like a sharp
  human editor, not a content generator.
"""


# ---------------------------------------------------------------------------
# 1. Categorize senders  ->  newsletter / marketing / ai / other
# ---------------------------------------------------------------------------

def categorize_senders(senders: list[dict]) -> dict[str, str]:
    if not senders:
        return {}
    listing = "\n".join(f"{s['email']} | {s.get('name','')}" for s in senders)
    prompt = (
        "Classify each email SENDER into exactly one category:\n"
        "- newsletter: editorial / curated content digests\n"
        "- marketing: promotional, sales, product offers\n"
        "- ai: specifically about AI / machine learning topics\n"
        "- other: anything else (transactional, personal, notifications)\n\n"
        f"Senders (email | display name):\n{listing}\n\n"
        'Respond ONLY with a JSON object mapping each email to its category, e.g. '
        '{"a@x.com":"newsletter","b@y.com":"ai"}'
    )
    try:
        raw = _complete(
            prompt, max_tokens=1500, model=config.CLAUDE_MODEL_LIGHT, tag="categorize_senders",
            description=f"Classify {len(senders)} onboarding-scan sender(s) into categories")
    except Exception:
        return {s["email"]: "other" for s in senders}  # no credits -> uncategorized
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {s["email"]: "other" for s in senders}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {s["email"]: "other" for s in senders}
    valid = {"newsletter", "marketing", "ai", "other"}
    return {k: (v if v in valid else "other") for k, v in data.items()}


# ---------------------------------------------------------------------------
# 2. Summarize source emails into ONE reviewable digest summary
# ---------------------------------------------------------------------------

def build_summary(emails: list[dict]) -> str:
    """Read every source newsletter directly and produce ONE research-backed brief -
    this is where research happens now, not at final-write time. One call for the
    whole batch (not one call per email + a merge call): same task, far fewer
    round-trips and no N-times-repeated instruction overhead.

    The brief itself is what gets emailed for approval, so the research is visible
    and reviewable before a single word of the final newsletter gets written."""
    sources = []
    for e in emails:
        content = e.get("text", "")
        if e.get("attachments_text"):
            content += "\n\n--- ATTACHMENTS ---\n" + e["attachments_text"]
        if content.strip():
            sources.append(
                f"### From {e.get('sender','')} — {e.get('subject','')}\n{content[:8000]}"
            )
    if not sources:
        return ""
    source_count = len(sources)
    joined = "\n\n".join(sources)
    prompt = (
        f"Below are {source_count} full newsletter emails - different senders, different "
        "topics/sub-topics, different angles. Read them directly (not a pre-made summary) "
        "and produce a DEEP-RESEARCH BRIEF that I will review before it becomes my "
        "newsletter.\n\n"
        "Work in this order:\n"
        f"1. Cover EVERY one of the {source_count} sources - read what each is actually "
        "saying, not just its headline.\n"
        "2. Identify the 3-5 real THEMES running across them, and for each theme note where "
        "the sources genuinely TOUCH - where they agree, disagree, or build on each other.\n"
        "3. Spot the GAPS: a fact, a number, recent development, or angle that matters to "
        "these themes but that none of the sources covered.\n"
        "4. Use web search to research those gaps and to verify/strengthen the sources' own "
        "claims with current, concrete facts, numbers, names, or quotes. This is what makes "
        "the brief genuinely deep instead of a repackage of what was already in these inboxes "
        "- search enough to back every major theme with something beyond the sources; don't "
        "search for things that don't add anything.\n"
        "5. Write the brief itself: organize by theme, merge overlapping source content, fold "
        "in what you researched, and surface the non-obvious takeaway a smart reader would "
        "want. Keep the genuinely useful links (both from sources and from research).\n\n"
        "Write this as an internal research brief on the TOPICS/THEMES themselves - state "
        "findings directly (\"Free-tier friction is becoming standard because...\"), not as a "
        "personal account of reading/discovering these particular source emails (\"here's "
        "what I dug into\", \"reading my inbox\"). This brief's own framing carries through to "
        "the final newsletter, so keep it topic-first here too. Plain text with clear theme "
        "headers and tight bullets. This is the BRIEF for approval, not the final formatted "
        "newsletter.\n\n"
        f"SOURCES:\n{joined}"
    )
    return _complete(
        prompt, max_tokens=8192, tools=[WEB_SEARCH_TOOL], effort=config.CLAUDE_WRITER_EFFORT,
        tag="build_summary",
        description=f"Research {source_count} source newsletter(s): themes, gaps, web search")


# ---------------------------------------------------------------------------
# 3. Turn a research brief into the newsletter (+ revise on reviewer feedback)
# ---------------------------------------------------------------------------

def _writer_system(brand_name: str = "", brand_signature: str = "") -> str:
    brand = ""
    if brand_name or brand_signature:
        brand = (
            "\n\nBRAND / VOICE:\n"
            f"- You are writing AS {brand_name or 'the sender'} — first person, their own "
            "curated newsletter to their audience. Warm, personal, confident.\n"
        )
        if brand_signature:
            brand += (
                "- Do NOT add a sign-off/signature block yourself - the platform appends "
                "one automatically after your content. End the body on your last content "
                "paragraph, no closing name or salutation line.\n"
            )
    return (
        "You are ghost-writing a personal curated newsletter in the sender's own voice "
        "(not a neutral roundup).\n\n"
        f"REPURPOSING RULES:\n{REPURPOSE_GUIDANCE}\n\n"
        f"ANTI-AI WRITING RULES:\n{NO_AI_GUIDANCE}"
        f"{brand}"
    )


_THEME_OPTIONS = ", ".join(THEMES.keys())

_EMOJI_RULE = (
    "Emoji: use them sparingly inside summary_points / feature.paragraphs / stories[].summary, "
    "only where they genuinely aid scanning - e.g. one emoji at the start of a point that flags "
    "a standout stat, tip, or warning (💡 insight, 📊 data point, ⚠️ caution). Never more than "
    "one per point, and skip them entirely where they'd feel decorative rather than useful. "
    "When in doubt, leave it out.\n"
)

_ALREADY_RESEARCHED_NOTE = (
    "The summary below is already a research-backed brief - the facts, numbers, and sources "
    "in it were found deliberately, not pulled from the source newsletters alone. Your job "
    "here is to write and structure the newsletter from it, not to research further.\n"
)

_NO_PROCESS_NARRATION_RULE = (
    "Never narrate how this newsletter was produced or where its material came from. "
    "Specifically forbidden anywhere in your output: mentioning an inbox, emails you "
    "'received'/'got'/'found'/'almost archived', reading or scanning mail, comparing emails "
    "'side by side', a 'rabbit hole' framing, 'this round', research tools, searches, or any "
    "other reference to the sourcing/research process. This is a newsletter about a topic, "
    "not a diary of how you researched it. Present findings directly, like an analyst who "
    "already knows the subject - companies/products you discuss are examples or case studies "
    "you're citing, never 'things that landed in my inbox'. This applies to every field, "
    "especially \"note\" and \"issue_tagline\" - both must name/frame the issue's TOPIC, not "
    "the research process behind it. First-person opinion is still fine (\"I think the real "
    "story here is...\") - it's the sourcing mechanism that must stay invisible, not your "
    "voice.\n"
)

_SECTIONS_SCHEMA_RULE = (
    "Respond in EXACTLY this format, three parts in order:\n"
    "THEME: <theme>\n"
    "SUBJECT: <subject>\n"
    "JSON:\n"
    "<a single JSON object, nothing else - no markdown fences, no prose before or after>\n\n"
    f"<theme> must be exactly one of: {_THEME_OPTIONS} - pick whichever best matches the "
    "dominant topic of THIS issue's content (not the sender categories); it selects the "
    "newsletter's visual color template.\n"
    "<subject> must be catchy, specific, natural English (5-9 words), reflecting the actual "
    "top story of THIS issue. No vague/generic subjects, no clickbait that doesn't match the "
    "content, no dates-only.\n\n"
    "The JSON object has this exact shape (all text fields are PLAIN TEXT, no HTML tags - the "
    "template supplies every tag and link):\n"
    "{\n"
    '  "issue_tagline": "short phrase, 4-8 words, naming this issue\'s TOPIC (e.g. \'Free-tier '
    'growth tactics, decoded\') - never a phrase about reading mail or researching",\n'
    '  "note": "2-4 sentence opening that states the topic and your thesis directly - first "\n'
    '          "person opinion is fine, narrating how you researched it is not; no greeting "\n'
    '          "line, the template renders the greeting itself",\n'
    '  "summary_points": ["3-5 short, scannable highlights from this issue"],\n'
    '  "feature": {\n'
    '    "category": "1-2 word label for the lead story\'s topic",\n'
    '    "headline": "the lead story\'s headline",\n'
    '    "dek": "one sentence sub-headline",\n'
    '    "paragraphs": ["2-4 paragraphs on the single most important/interesting theme, '
    'incorporating your research"],\n'
    '    "cta_label": "optional short call-to-action label, e.g. \'Read the full story\' - '
    'omit if no single strong link fits",\n'
    '    "cta_url": "the real URL for cta_label - omit together with cta_label if none"\n'
    "  },\n"
    '  "stories": [\n'
    '    {"tag": "1-2 word category", "title": "...", "summary": "1-3 sentences", '
    '"source": "who/where this came from", "url": "optional real URL"}\n'
    "    // 2-4 of these: the OTHER themes from the summary, each as its own short story\n"
    "  ],\n"
    '  "quote": {"text": "a genuinely striking quote or stat you found", '
    '"attribution": "who said/found it", "role": "their role/context"} '
    "// omit this key entirely if nothing strong enough turned up - never invent one,\n"
    '  "further_reading": [{"title": "...", "url": "real URL"}] '
    "// 2-4 of the best sources you actually found - omit/empty if none\n"
    "}\n"
)


def _extract_json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def _normalize_sections(data: dict) -> dict:
    feature = data.get("feature") or {}
    return {
        "issue_tagline": str(data.get("issue_tagline") or ""),
        "note": str(data.get("note") or ""),
        "summary_points": [str(p) for p in (data.get("summary_points") or []) if str(p).strip()],
        "feature": {
            "category": str(feature.get("category") or ""),
            "headline": str(feature.get("headline") or ""),
            "dek": str(feature.get("dek") or ""),
            "paragraphs": [str(p) for p in (feature.get("paragraphs") or []) if str(p).strip()],
            "cta_label": str(feature.get("cta_label") or ""),
            "cta_url": str(feature.get("cta_url") or ""),
        },
        "stories": [
            {
                "tag": str(s.get("tag") or ""), "title": str(s.get("title") or ""),
                "summary": str(s.get("summary") or ""), "source": str(s.get("source") or ""),
                "url": str(s.get("url") or ""),
            }
            for s in (data.get("stories") or []) if isinstance(s, dict) and s.get("title")
        ],
        "quote": (
            {
                "text": str(data["quote"].get("text") or ""),
                "attribution": str(data["quote"].get("attribution") or ""),
                "role": str(data["quote"].get("role") or ""),
            }
            if isinstance(data.get("quote"), dict) and data["quote"].get("text") else None
        ),
        "further_reading": [
            {"title": str(l.get("title") or ""), "url": str(l.get("url") or "")}
            for l in (data.get("further_reading") or []) if isinstance(l, dict) and l.get("url")
        ],
    }


def _parse_newsletter_response(raw: str) -> dict:
    theme, subject = DEFAULT_THEME, "Your newsletter digest"
    m = re.search(r"THEME:\s*(\S+)", raw)
    if m and m.group(1).strip().lower() in THEMES:
        theme = m.group(1).strip().lower()
    m = re.search(r"SUBJECT:\s*(.+)", raw)
    if m:
        subject = m.group(1).strip()
    return {"theme": theme, "subject": subject, "sections": _normalize_sections(_extract_json(raw))}


def build_newsletter_from_summary(summary: str, brand_name: str = "", brand_signature: str = "") -> dict:
    prompt = (
        "Using the approved summary below as your starting point, prepare my personal curated "
        "newsletter content about the topic(s) it covers, first person, as an analyst who "
        "already knows this subject writing directly to their audience.\n"
        f"{_ALREADY_RESEARCHED_NOTE}"
        f"{_NO_PROCESS_NARRATION_RULE}"
        f"{_EMOJI_RULE}"
        f"{_SECTIONS_SCHEMA_RULE}\n"
        f"APPROVED SUMMARY:\n{summary}"
    )
    return _parse_newsletter_response(_complete(
        prompt, max_tokens=8192, system=_writer_system(brand_name, brand_signature),
        effort=config.CLAUDE_WRITER_EFFORT, tag="build_newsletter", cache_system=True,
        description="Write the first newsletter draft from the researched brief"))


def revise_newsletter(current_sections: dict, feedback: str, subject: str = "", theme: str = DEFAULT_THEME,
                      brand_name: str = "", brand_signature: str = "") -> dict:
    prompt = (
        "Revise the newsletter content below based on my requested changes. If the changes "
        "call for new or more current information, use web search to find it - real facts and "
        "URLs only, never invented ones.\n"
        f"{_NO_PROCESS_NARRATION_RULE}"
        f"{_EMOJI_RULE}"
        f"Keep THEME as '{theme}' unless the requested changes clearly call for a different "
        f"topic focus — if so, pick the best fit from: {_THEME_OPTIONS}.\n"
        f"{_SECTIONS_SCHEMA_RULE}\n"
        f"REQUESTED CHANGES:\n{feedback}\n\n"
        f"CURRENT SUBJECT: {subject}\n"
        f"CURRENT CONTENT JSON:\n{json.dumps(current_sections)}"
    )
    return _parse_newsletter_response(_complete(
        prompt, max_tokens=8192, system=_writer_system(brand_name, brand_signature),
        tools=[WEB_SEARCH_TOOL], effort=config.CLAUDE_WRITER_EFFORT, tag="revise_newsletter",
        cache_system=True,
        description=f"Revise newsletter draft per reviewer feedback: {feedback[:80]!r}"))
