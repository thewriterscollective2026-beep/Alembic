"""Claude-powered categorization, summarization, and newsletter repurposing.

The repurpose + "no-AI-writing" guidance below is embedded as prompt text because
Claude Code *skills* don't run inside a deployed web app. To use the exact wording
of your own /repurpose and /no-ai skills, paste their text into the two constants
marked >>> PASTE ... <<< below.
"""
import json
import logging
import re
import anthropic

from . import config
from .email_template import THEMES, DEFAULT_THEME

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
              effort: str | None = None, tag: str = "") -> str:
    """Call Claude and return the joined visible text. Every call is tagged and
    its token usage logged (input/output/cache), so spend is measurable instead
    of guessed - check the `ai_service.usage` logger."""
    kwargs = {}
    if tools:
        kwargs["tools"] = tools
    if effort:
        kwargs["output_config"] = {"effort": effort}
    msg = _get_client().messages.create(
        model=model or config.CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system or "You are a precise, helpful assistant.",
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    u = msg.usage
    _usage_log.info(
        "tag=%s model=%s input=%d output=%d cache_read=%d cache_write=%d",
        tag or "untagged", msg.model, u.input_tokens, u.output_tokens,
        getattr(u, "cache_read_input_tokens", 0) or 0,
        getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    # Server tool blocks (e.g. web_search_tool_result) are skipped here on purpose -
    # only the model's own writing ends up in the joined text.
    return "".join(block.text for block in msg.content if block.type == "text").strip()


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
        raw = _complete(prompt, max_tokens=1500, model=config.CLAUDE_MODEL_LIGHT, tag="categorize_senders")
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
    """Summarize every source email AND merge into ONE digest that covers EVERY
    source (nothing dropped), framed as the reader's own curated findings.

    One call for the whole batch (not one call per email + a merge call) - same
    task, far fewer round-trips and no N-times-repeated instruction overhead."""
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
        f"Below are {source_count} full newsletter emails. Produce a DEEP-RESEARCH BRIEF "
        "that I will review before it becomes my newsletter — not a shallow summary.\n\n"
        "For the brief:\n"
        f"- Cover EVERY one of the {source_count} sources; merge overlaps, omit none.\n"
        "- Identify the 3-5 real THEMES running across the sources and organise by theme.\n"
        "- For each theme: what it's actually about, WHY it matters right now, the concrete "
        "facts / numbers / names / quotes that back it, and any connection between sources.\n"
        "- Surface the non-obvious angle or takeaway a smart reader would want.\n"
        "- Keep the genuinely useful links.\n"
        "- Write in first person, as MY own researched findings (\"Here's what I dug into...\").\n"
        "- Plain text with clear theme headers and tight bullets. This is the BRIEF for "
        "approval, not the final formatted newsletter.\n\n"
        f"SOURCES:\n{joined}"
    )
    return _complete(prompt, max_tokens=3000, model=config.CLAUDE_MODEL_LIGHT, tag="build_summary")


def revise_summary(current: str, feedback: str) -> str:
    prompt = (
        "Revise the SUMMARY below based on the reader's requested changes. Keep the "
        "same plain-text digest style. Return only the revised summary.\n\n"
        f"REQUESTED CHANGES:\n{feedback}\n\n"
        f"CURRENT SUMMARY:\n{current}"
    )
    return _complete(prompt, max_tokens=1800, model=config.CLAUDE_MODEL_LIGHT, tag="revise_summary")


# ---------------------------------------------------------------------------
# 3. Turn an APPROVED summary into the newsletter (+ revise on feedback)
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

_RESEARCH_RULE = (
    "Before writing, use web search to RESEARCH the real themes in the summary below - find "
    "current, concrete facts, numbers, examples, or developments that go beyond what the "
    "summary already says. This is what makes the newsletter genuinely new instead of a "
    "repackage of the source newsletters. Search enough to back at least one solid point per "
    "major theme; don't pad with searches that don't add anything. If a search turns up "
    "nothing useful for a theme, proceed with the summary content alone for that theme.\n"
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
    '  "issue_tagline": "short phrase, 4-8 words, describing this issue\'s focus",\n'
    '  "note": "2-4 sentence opening from you to the reader, first person, no greeting line "\n'
    '          "(the template renders the greeting itself) - what you found and why it matters",\n'
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
        "newsletter content, first person (as if I wrote it and I'm sharing what I found).\n"
        f"{_RESEARCH_RULE}"
        f"{_EMOJI_RULE}"
        f"{_SECTIONS_SCHEMA_RULE}\n"
        f"APPROVED SUMMARY:\n{summary}"
    )
    return _parse_newsletter_response(_complete(
        prompt, max_tokens=8192, system=_writer_system(brand_name, brand_signature),
        tools=[WEB_SEARCH_TOOL], effort=config.CLAUDE_WRITER_EFFORT, tag="build_newsletter"))


def revise_newsletter(current_sections: dict, feedback: str, subject: str = "", theme: str = DEFAULT_THEME,
                      brand_name: str = "", brand_signature: str = "") -> dict:
    prompt = (
        "Revise the newsletter content below based on my requested changes. If the changes "
        "call for new or more current information, use web search to find it - real facts and "
        "URLs only, never invented ones.\n"
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
        tools=[WEB_SEARCH_TOOL], effort=config.CLAUDE_WRITER_EFFORT, tag="revise_newsletter"))
