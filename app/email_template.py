"""Render the newsletter as a structured, editorial-style HTML email.

Adapted from a Claude Design template the user built ("Organic" design system:
warm cream/terracotta/sage, Georgia display headings, rounded cards, subtle
fade-in/hover polish that degrades safely in clients that ignore <style>).
Table-based layout with inline styles for baseline compatibility across
Gmail / Outlook / Apple Mail; the <style> block only adds progressive-enhancement
touches (animation, hover) on top of content that already looks right without it.

Content arrives as STRUCTURED DATA (see ai_service.py's JSON contract), not
freeform HTML - this module owns every tag. No photos/illustrations by design
(out of scope) - visual appeal comes from color, typography, whitespace and
hierarchy, per https://mailchimp.com/resources/email-newsletter-format/.

Three color THEMES are variants of this one structural layout - the newsletter-
writing prompt in ai_service.py picks one per issue based on its dominant topic.
The {{FIRST_NAME}} token is rendered directly into the greeting by this module
(not authored by the model) and is filled in per-recipient at send time.

Two structural LAYOUTS share that same THEMES palette and the same content-section
renderers (note/summary/feature/stories/quote/further_reading/signature) - only the
masthead and footer differ:
  - "editorial" (default): dark full-bleed masthead, the original design.
  - "banner": light utility-bar + centered wordmark + author byline masthead,
    modeled on a Substack/Beehiiv-style newsletter (date + "read online",
    brand wordmark, headline, emoji-flagged kicker, author avatar+name, an
    optional sponsor strip, and a social-icon footer bar). The author avatar is
    a CSS-drawn initial circle (no image asset), consistent with this module's
    no-photos design. The sponsor strip is scaffolding for a future ad-sales
    feature - pass `sponsor` to render it; omitted (None) by default, so it's
    invisible until that feature is built.
`User.newsletter_layout` picks the layout (a brand/structural choice, set once
in Settings) - unlike `theme` (color), which Claude still picks per issue.
"""
import html as _html
import re

GREETING_TOKEN = "{{FIRST_NAME}}"
# Filled in per-recipient at send time (resend_service.py) with that contact's
# real one-click unsubscribe link - same pattern as GREETING_TOKEN. Only Resend
# sends resolve it; drafts/previews sent to the client show the literal token,
# same as {{FIRST_NAME}} does everywhere except the real final send.
UNSUBSCRIBE_TOKEN = "{{UNSUBSCRIBE_URL}}"
INK = "#201e1d"
MUTED = "#6e6659"
LINE = "#dbcfb8"

# Each theme is a full color variant of the same structural layout: `page`/`card`
# are the page and card-shell backgrounds, `dark` is the masthead/dark-card bg,
# `accent` drives CTAs/kickers/badges, `accent2` drives section eyebrow labels.
THEMES = {
    "general": {
        "page": "#efe2cc", "card": "#f5ead8", "card_alt": "#fffaf1", "dark": "#201e1d",
        "accent": "#c67139", "accent2": "#7a8a5e", "chip": "#f0dcc8",
        "label": "Weekly Read", "emoji": "\U0001F4F0",
    },
    "tech": {
        "page": "#eef1f8", "card": "#f7f9ff", "card_alt": "#ffffff", "dark": "#1c1e2b",
        "accent": "#4f46e5", "accent2": "#0f766e", "chip": "#e3e6fb",
        "label": "Tech Digest", "emoji": "\U0001F9E0",
    },
    "business": {
        "page": "#eaf2ec", "card": "#f6fbf7", "card_alt": "#ffffff", "dark": "#16241c",
        "accent": "#0f5132", "accent2": "#9a7b2f", "chip": "#dfeee4",
        "label": "Business Brief", "emoji": "\U0001F4C8",
    },
}
DEFAULT_THEME = "general"

# Websafe stacks only - a custom note is one of the few hand-authored (not
# AI-written) blocks in the email, so its font choice must render consistently
# across Gmail/Outlook/Apple Mail without a web-font request, which most mail
# clients strip anyway. Keys are what Settings/`User.custom_note_font` store.
CUSTOM_NOTE_FONTS = {
    "default": "Georgia,'Times New Roman',serif",
    "serif": "Georgia,'Times New Roman',serif",
    "sans": "Arial,Helvetica,sans-serif",
    "mono": "'Courier New',Courier,monospace",
    "elegant": "'Trebuchet MS',Verdana,sans-serif",
}
DEFAULT_CUSTOM_NOTE_FONT = "default"

# Where the custom note can be inserted relative to the AI-written sections.
# Keys are what Settings/`User.custom_note_position` store; values are the
# section keys in render_email's body-section list it's inserted before.
CUSTOM_NOTE_POSITIONS = {
    "top": "note",
    "after_summary": "feature",
    "before_signature": "signature",
    "bottom": None,  # appended after every other section, before the footer
}
DEFAULT_CUSTOM_NOTE_POSITION = "top"


def _theme(name: str) -> dict:
    return THEMES.get((name or "").lower(), THEMES[DEFAULT_THEME])


def _e(value) -> str:
    """HTML-escape a plain-text content field (all AI-authored fields are plain text)."""
    return _html.escape(str(value or ""))


def _preheader_text(sections: dict, max_chars: int = 110) -> str:
    text = (sections.get("note") or "").strip()
    return text[:max_chars]


def _row_spacer(px: int) -> str:
    return f'<tr><td style="height:{px}px;line-height:{px}px;font-size:0">&nbsp;</td></tr>'


def _eyebrow(text: str, color: str) -> str:
    return (
        f'<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
        f'line-height:14px;letter-spacing:.16em;text-transform:uppercase;color:{color};'
        f'font-weight:bold;padding-bottom:12px">{_e(text)}</td></tr>'
    )


def _masthead(t: dict, kicker: str, issue_number: str, headline: str,
              issue_date: str, issue_tagline: str) -> str:
    return f"""
  <tr><td class="nl-pad nl-fade" style="padding:34px 40px 26px 40px;background-color:{t['dark']}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td align="left" style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:16px;
          letter-spacing:.16em;text-transform:uppercase;color:{t['accent']};font-weight:bold">{_e(kicker)}</td>
        <td align="right" style="font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:16px;
          letter-spacing:.10em;text-transform:uppercase;color:#b3a894">{_e(issue_number)}</td>
      </tr>
      {_row_spacer(14)}
      <tr><td colspan="2" align="left" class="nl-h1" style="font-family:Georgia,'Times New Roman',serif;
        font-size:34px;line-height:40px;color:{t['card']};font-weight:normal">{_e(headline)}</td></tr>
      {_row_spacer(12)}
      <tr><td colspan="2" align="left" style="font-family:Arial,Helvetica,sans-serif;font-size:14px;
        line-height:20px;color:#c9bda8">{_e(issue_date)} &nbsp;&middot;&nbsp; {_e(issue_tagline)}</td></tr>
    </table>
  </td></tr>"""


def _initial_avatar(t: dict, name: str, avatar_url: str = "", size: int = 44) -> str:
    """The author's uploaded photo if `avatar_url` is set, otherwise a CSS-drawn
    circle with their first initial - a real photo is an explicit opt-in
    (Settings -> upload), not the default (see module docstring)."""
    if avatar_url.strip():
        return (
            f'<td width="{size}" style="width:{size}px;height:{size}px">'
            f'<img src="{_html.escape(avatar_url)}" width="{size}" height="{size}" alt="{_e(name)}" '
            f'style="width:{size}px;height:{size}px;border-radius:50%;display:block;object-fit:cover"></td>'
        )
    initial = (name or "?").strip()[:1].upper() or "?"
    return (
        f'<td width="{size}" style="width:{size}px;height:{size}px;border-radius:50%;'
        f'background-color:{t["accent"]};font-family:Arial,Helvetica,sans-serif;'
        f'font-size:{int(size*0.42)}px;line-height:{size}px;color:#ffffff;font-weight:bold;'
        f'text-align:center" align="center" valign="middle">{_e(initial)}</td>'
    )


def _banner_masthead(t: dict, brand_name: str, label: str, emoji: str, issue_date: str,
                      headline: str, issue_tagline: str, author_name: str,
                      read_online_url: str, sponsor: dict | None, avatar_url: str = "") -> str:
    read_online = ""
    if read_online_url.strip():
        read_online = (f' &nbsp;|&nbsp; <a class="nl-link" href="{_html.escape(read_online_url)}" '
                       f'style="color:{t["accent"]};text-decoration:underline">Read online</a>')
    sponsor_html = ""
    if sponsor and sponsor.get("name"):
        sponsor_label = _e(sponsor.get("label") or "In partnership with")
        sponsor_html = f"""
      {_row_spacer(26)}
      <tr><td align="center" style="font-family:Arial,Helvetica,sans-serif;font-size:12px;
        line-height:16px;color:{MUTED}">{sponsor_label}</td></tr>
      {_row_spacer(6)}
      <tr><td align="center" style="font-family:Georgia,'Times New Roman',serif;font-size:19px;
        line-height:24px;color:{INK};font-weight:bold">{_e(sponsor['name'])}</td></tr>"""
    return f"""
  <tr><td class="nl-pad nl-fade" style="padding:24px 40px 30px 40px;background-color:{t['chip']};
    border-bottom:1px solid {LINE}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td align="right" style="font-family:Arial,Helvetica,sans-serif;font-size:12px;
          line-height:18px;color:{MUTED}">{_e(issue_date)}{read_online}</td>
      </tr>
      {_row_spacer(22)}
      <tr><td align="center" style="font-family:Georgia,'Times New Roman',serif;font-size:30px;
        line-height:34px;color:{t['accent']};font-weight:bold">{_e(brand_name or 'Newsletter')}</td></tr>
      <tr><td align="center" style="font-family:Arial,Helvetica,sans-serif;font-size:11px;
        line-height:18px;letter-spacing:.22em;text-transform:uppercase;color:{INK};
        font-weight:bold">{_e(label)}</td></tr>
      {_row_spacer(26)}
      <tr><td align="center" style="font-family:Georgia,'Times New Roman',serif;font-size:24px;
        line-height:31px;color:{INK}">{_e(emoji)}&nbsp; {_e(headline)}</td></tr>
      {_row_spacer(10) if issue_tagline.strip() else ""}
      {f'<tr><td align="center" style="font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:24px;color:{MUTED}">{_e(issue_tagline)}</td></tr>' if issue_tagline.strip() else ""}
      {_row_spacer(20)}
      <tr><td align="center">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          {_initial_avatar(t, author_name, avatar_url)}
          <td width="10" style="width:10px">&nbsp;</td>
          <td valign="middle" style="font-family:Arial,Helvetica,sans-serif;font-size:14px;
            line-height:18px;color:{INK};font-weight:bold">{_e(author_name or brand_name)}</td>
        </tr></table>
      </td></tr>
      {sponsor_html}
    </table>
  </td></tr>"""


def _banner_footer(t: dict, brand_name: str, address_line: str, unsub: str,
                    social_links: dict | None) -> str:
    social_html = ""
    social_links = social_links or {}
    labels = {"instagram": "IG", "youtube": "YT", "linkedin": "in", "twitter": "X", "x": "X"}
    icons = "".join(
        f'<td style="padding:0 6px"><a href="{_html.escape(url)}" '
        f'style="display:inline-block;width:30px;height:30px;line-height:30px;border-radius:50%;'
        f'background-color:{t["dark"]};color:#ffffff;font-family:Arial,Helvetica,sans-serif;'
        f'font-size:11px;font-weight:bold;text-align:center;text-decoration:none">'
        f'{labels.get(key, key[:2].upper())}</a></td>'
        for key, url in social_links.items() if url
    )
    if icons:
        social_html = f"""
      <tr><td align="center" style="padding-bottom:20px">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>{icons}</tr></table>
      </td></tr>"""
    return f"""
  <tr><td class="nl-pad" style="padding:30px 40px 36px 40px;background-color:{t['card_alt']};
    border-top:1px solid {LINE}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      {social_html}
      <tr><td align="center" style="font-family:Arial,Helvetica,sans-serif;font-size:12px;
        line-height:20px;color:{MUTED}">
        Update your email preferences or
        <a class="nl-link" href="{_html.escape(unsub)}" style="color:{t['accent']};text-decoration:underline">unsubscribe</a>
      </td></tr>
      {_row_spacer(10)}
      <tr><td align="center" style="font-family:Arial,Helvetica,sans-serif;font-size:12px;
        line-height:20px;color:{MUTED}">&copy; {_e(_dt_year())} {_e(brand_name or 'Newsletter')}{address_line}</td></tr>
    </table>
  </td></tr>"""


def _dt_year() -> str:
    import datetime as _dt
    return str(_dt.datetime.utcnow().year)


def _note_section(t: dict, note: str) -> str:
    if not note.strip():
        return ""
    return f"""
  <tr><td class="nl-pad nl-fade nl-d1" style="padding:34px 40px 6px 40px;background-color:{t['card']}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      {_eyebrow("This issue", t['accent2'])}
      <tr><td style="font-family:Georgia,'Times New Roman',serif;font-size:19px;line-height:30px;
        color:{INK}">Hi {GREETING_TOKEN},</td></tr>
      {_row_spacer(10)}
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:27px;
        color:#3c3833">{_e(note)}</td></tr>
    </table>
  </td></tr>"""


def _summary_section(t: dict, points: list) -> str:
    if not points:
        return ""
    rows = "".join(
        f'<tr><td width="30" valign="top" style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;'
        f'font-size:13px;line-height:22px;color:{t["accent2"]};font-weight:bold">{i}</td>'
        f'<td valign="top" style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:16px;'
        f'line-height:25px;color:#2c2925">{_e(p)}</td></tr>'
        for i, p in enumerate(points, 1)
    )
    return f"""
  <tr><td class="nl-pad nl-fade nl-d2" style="padding:30px 40px 8px 40px;background-color:{t['card']}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" class="nl-card"
      style="background-color:{t['card_alt']};border-radius:16px;box-shadow:0 4px 14px rgba(32,30,29,.06)">
      <tr><td style="padding:26px 26px 8px 26px;font-family:Arial,Helvetica,sans-serif;font-size:11px;
        line-height:14px;letter-spacing:.16em;text-transform:uppercase;color:{t['accent']};
        font-weight:bold">What's in this issue</td></tr>
      <tr><td style="padding:0 26px 22px 26px">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{rows}</table>
      </td></tr>
    </table>
  </td></tr>"""


def _feature_section(t: dict, feature: dict) -> str:
    if not feature or not feature.get("headline"):
        return ""
    paragraphs = "".join(
        f'<tr><td style="padding-top:16px;font-family:Arial,Helvetica,sans-serif;font-size:16px;'
        f'line-height:27px;color:#3c3833">{_e(p)}</td></tr>'
        for p in feature.get("paragraphs") or []
    )
    cta = ""
    if feature.get("cta_label") and feature.get("cta_url"):
        cta = f"""
      <tr><td style="padding-top:24px">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td bgcolor="{t['accent']}" style="border-radius:999px">
            <a class="nl-btn" href="{_html.escape(feature['cta_url'])}" style="display:block;padding:14px 30px;
              font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:18px;font-weight:bold;
              color:#ffffff;text-decoration:none;border-radius:999px">{_e(feature['cta_label'])}</a>
          </td>
        </tr></table>
      </td></tr>"""
    category = ""
    if feature.get("category"):
        category = f"""
      <tr><td style="padding-bottom:14px">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="background-color:{t['chip']};border-radius:999px;padding:7px 16px;
            font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:14px;letter-spacing:.14em;
            text-transform:uppercase;color:{t['accent']};font-weight:bold">{_e(feature['category'])}</td>
        </tr></table>
      </td></tr>"""
    dek = ""
    if feature.get("dek"):
        dek = (f'<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:17px;line-height:28px;'
               f'color:#4e4842">{_e(feature["dek"])}</td></tr>')
    return f"""
  <tr><td class="nl-pad nl-fade nl-d3" style="padding:30px 40px 6px 40px;background-color:{t['card']}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      {category}
      <tr><td style="font-family:Georgia,'Times New Roman',serif;font-size:27px;line-height:35px;
        color:{INK}">{_e(feature['headline'])}</td></tr>
      {_row_spacer(12)}
      {dek}
      {paragraphs}
      {cta}
    </table>
  </td></tr>"""


def _stories_section(t: dict, stories: list) -> str:
    if not stories:
        return ""
    cards = "".join(f"""
      <tr><td style="padding:14px 0">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" class="nl-card"
          style="background-color:{t['card_alt']};border-radius:16px;box-shadow:0 3px 12px rgba(32,30,29,.05)">
          <tr><td style="padding:22px 24px">
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:14px;
              letter-spacing:.12em;text-transform:uppercase;color:{t['accent']};font-weight:bold;
              padding-bottom:8px">{_e(s.get('tag',''))}</div>
            <div style="font-family:Georgia,'Times New Roman',serif;font-size:19px;line-height:27px;
              color:{INK}">{_e(s.get('title',''))}</div>
            <div style="height:8px;line-height:8px;font-size:0">&nbsp;</div>
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:25px;
              color:#4e4842">{_e(s.get('summary',''))}</div>
            <div style="height:12px;line-height:12px;font-size:0">&nbsp;</div>
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;color:{MUTED}">
              {_e(s.get('source',''))}
              {f' &nbsp;&middot;&nbsp; <a class="nl-link" href="{_html.escape(s["url"])}" style="color:{t["accent"]};text-decoration:underline">Read more</a>' if s.get('url') else ''}
            </div>
          </td></tr>
        </table>
      </td></tr>""" for s in stories)
    return f"""
  <tr><td class="nl-pad nl-fade nl-d4" style="padding:32px 40px 6px 40px;background-color:{t['card']}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      {_eyebrow("Also worth your time", t['accent2'])}
      {cards}
    </table>
  </td></tr>"""


def _quote_section(t: dict, quote: dict) -> str:
    if not quote or not quote.get("text"):
        return ""
    attribution = quote.get("attribution", "")
    role = quote.get("role", "")
    sep = " &nbsp;&middot;&nbsp; " if attribution and role else ""
    return f"""
  <tr><td class="nl-pad nl-fade nl-d5" style="padding:32px 40px 10px 40px;background-color:{t['card']}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr><td style="font-family:Georgia,'Times New Roman',serif;font-size:40px;line-height:40px;
        color:{t['accent']}">&ldquo;</td></tr>
      <tr><td style="font-family:Georgia,'Times New Roman',serif;font-size:21px;line-height:32px;
        color:{INK}">{_e(quote['text'])}</td></tr>
      {_row_spacer(14)}
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:20px;color:{MUTED}">
        <strong style="color:{INK}">{_e(attribution)}</strong>{sep}{_e(role)}</td></tr>
    </table>
  </td></tr>"""


def _further_reading_section(t: dict, links: list) -> str:
    if not links:
        return ""
    items = "".join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid {LINE};font-family:Arial,Helvetica,'
        f'sans-serif;font-size:15px;line-height:22px"><a class="nl-link" href="{_html.escape(l.get("url",""))}" '
        f'style="color:{t["accent"]};text-decoration:underline">{_e(l.get("title") or l.get("url"))}</a></td></tr>'
        for l in links if l.get("url")
    )
    if not items:
        return ""
    return f"""
  <tr><td class="nl-pad nl-fade nl-d6" style="padding:28px 40px 8px 40px;background-color:{t['card']}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      {_eyebrow("Further reading", t['accent2'])}
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{items}</table>
    </table>
  </td></tr>"""


def _custom_note_section(t: dict, title: str, text: str, font_key: str) -> str:
    """Owner-authored callout (not AI-written) - see User.custom_note_* and
    CUSTOM_NOTE_FONTS/CUSTOM_NOTE_POSITIONS above. Styled like a pulled-quote
    card so it reads as a deliberate aside, not a stray paragraph."""
    if not text.strip():
        return ""
    font = CUSTOM_NOTE_FONTS.get(font_key, CUSTOM_NOTE_FONTS[DEFAULT_CUSTOM_NOTE_FONT])
    body_html = _e(text.strip()).replace("\n", "<br>")
    title_html = (
        f'<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
        f'line-height:14px;letter-spacing:.14em;text-transform:uppercase;color:{t["accent"]};'
        f'font-weight:bold;padding-bottom:10px">{_e(title)}</td></tr>'
        if title.strip() else ""
    )
    return f"""
  <tr><td class="nl-pad" style="padding:24px 40px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
      style="background-color:{t['chip']};border-radius:14px">
      <tr><td style="padding:20px 22px">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
          {title_html}
          <tr><td style="font-family:{font};font-size:16px;line-height:26px;
            color:{INK}">{body_html}</td></tr>
        </table>
      </td></tr>
    </table>
  </td></tr>"""


def _signature_section(t: dict, brand_signature: str) -> str:
    if not brand_signature.strip():
        return ""
    sig_html = _e(brand_signature.strip()).replace("\n", "<br>")
    return f"""
  <tr><td class="nl-pad" style="padding:6px 40px 4px 40px;background-color:{t['card']}">
    <div style="border-top:1px solid {LINE};padding-top:18px;color:{INK};font-size:14px;
      line-height:1.6;font-family:Arial,Helvetica,sans-serif">{sig_html}</div>
  </td></tr>"""


def render_email(sections: dict, *, brand_name: str = "", brand_signature: str = "",
                 subscribe_url: str = "", unsubscribe_url: str = "", address: str = "",
                 from_email: str = "", theme: str = DEFAULT_THEME, issue_number: str = "",
                 subject: str = "", issue_date: str = "", layout: str = "editorial",
                 author_name: str = "", read_online_url: str = "",
                 sponsor: dict | None = None, social_links: dict | None = None,
                 avatar_url: str = "", custom_note: dict | None = None) -> str:
    """Return a full, styled HTML email built from structured `sections` data.

    `layout` picks the masthead/footer structure ("editorial" or "banner" - see
    module docstring); `author_name`, `read_online_url`, `sponsor`, `social_links`,
    `avatar_url` only affect the "banner" layout and are all optional/no-op on
    "editorial". `avatar_url` must be a publicly reachable image URL (a recipient's
    mail client fetches it directly) - omit it to fall back to the CSS initial circle.

    `custom_note` is an owner-authored callout (User.custom_note_*, Settings-
    controlled, not AI-written) - {"title", "text", "font", "position"} - or
    None/omitted to render nothing. See CUSTOM_NOTE_FONTS/CUSTOM_NOTE_POSITIONS.
    """
    import datetime as _dt
    t = _theme(theme)
    sections = sections or {}
    kicker = brand_name or "Newsletter"
    headline = subject or sections.get("headline") or "Your newsletter"
    now = _dt.datetime.utcnow()
    date_str = issue_date or f"{now:%A}, {now.day} {now:%B %Y}"
    # Default to the per-recipient placeholder (resend_service.py fills in the real
    # signed link at send time); an explicit unsubscribe_url always wins if passed.
    unsub = unsubscribe_url.strip() or UNSUBSCRIBE_TOKEN
    address_line = f'<br>{_e(address)}' if address.strip() else ""
    preheader = _preheader_text(sections)
    preheader_pad = "&nbsp;&zwnj;" * 40
    is_banner = (layout or "").lower() == "banner"

    body_parts = [
        ("note", _note_section(t, sections.get("note", ""))),
        ("summary", _summary_section(t, sections.get("summary_points") or [])),
        ("feature", _feature_section(t, sections.get("feature") or {})),
        ("stories", _stories_section(t, sections.get("stories") or [])),
        ("quote", _quote_section(t, sections.get("quote") or {})),
        ("further_reading", _further_reading_section(t, sections.get("further_reading") or [])),
        ("signature", _signature_section(t, brand_signature)),
    ]
    if custom_note and (custom_note.get("text") or "").strip():
        note_html = _custom_note_section(
            t, custom_note.get("title", ""), custom_note.get("text", ""),
            custom_note.get("font", DEFAULT_CUSTOM_NOTE_FONT),
        )
        before_key = CUSTOM_NOTE_POSITIONS.get(
            custom_note.get("position", DEFAULT_CUSTOM_NOTE_POSITION), "note")
        idx = next((i for i, (k, _) in enumerate(body_parts) if k == before_key), len(body_parts))
        body_parts.insert(idx, ("custom_note", note_html))
    body_sections = "".join(html for _, html in body_parts)

    if is_banner:
        masthead_html = _banner_masthead(
            t, brand_name, t["label"], t["emoji"], date_str, headline,
            sections.get("issue_tagline", ""), author_name or brand_name,
            read_online_url, sponsor, avatar_url,
        )
        footer_html = _banner_footer(t, brand_name, address_line, unsub, social_links)
    else:
        masthead_html = _masthead(t, kicker, issue_number, headline, date_str, sections.get("issue_tagline", ""))
        footer_html = f"""
  <tr><td class="nl-pad" style="padding:34px 40px 36px 40px;background-color:{t['card_alt']};border-top:1px solid {LINE}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:22px;color:{MUTED}">
        You're receiving this because you subscribed to updates from {_e(brand_name or from_email or 'us')}.</td></tr>
      {_row_spacer(16)}
      <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:20px;color:{MUTED}">
        <strong style="color:{INK}">{_e(brand_name or 'Newsletter')}</strong>{address_line}<br>
        <a class="nl-link" href="{_html.escape(unsub)}" style="color:{t['accent']};text-decoration:underline">Unsubscribe</a>
        &nbsp;&middot;&nbsp;
        <a class="nl-link" href="{_html.escape(subscribe_url or '#')}" style="color:{t['accent']};text-decoration:underline">Subscribe</a>
      </td></tr>
    </table>
  </td></tr>"""

    return f"""\
<!-- Alembic by IrizPro -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{t['page']}">
  {_e(preheader)}{preheader_pad}
</div>
<style>
  @media (prefers-reduced-motion: no-preference) {{
    @keyframes nlFade {{ from {{ opacity:0; transform:translateY(8px) }} to {{ opacity:1; transform:none }} }}
    .nl-fade {{ animation: nlFade .55s ease-out both }}
    .nl-d1 {{ animation-delay:.05s }} .nl-d2 {{ animation-delay:.12s }} .nl-d3 {{ animation-delay:.19s }}
    .nl-d4 {{ animation-delay:.26s }} .nl-d5 {{ animation-delay:.33s }} .nl-d6 {{ animation-delay:.40s }}
  }}
  .nl-link {{ transition: color .18s ease }}
  .nl-link:hover {{ color:{t['dark']} !important }}
  .nl-btn:hover {{ opacity:.9 }}
  .nl-card {{ transition: box-shadow .2s ease }}
  .nl-card:hover {{ box-shadow:0 10px 26px rgba(32,30,29,.12) !important }}
  @media only screen and (max-width:620px) {{
    .nl-shell {{ width:100% !important }}
    .nl-pad {{ padding-left:22px !important; padding-right:22px !important }}
    .nl-h1 {{ font-size:28px !important; line-height:34px !important }}
  }}
</style>
<div style="background-color:{t['page']};margin:0;padding:24px 0;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:{t['page']}">
<tr><td align="center">
<table role="presentation" class="nl-shell" cellpadding="0" cellspacing="0" border="0" width="600"
  style="width:600px;max-width:600px;background-color:{t['card']};border-radius:16px;overflow:hidden;
  box-shadow:0 12px 34px rgba(32,30,29,.10)">

{masthead_html}
{body_sections}

  <!-- footer -->
{footer_html}

</table>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="width:600px;max-width:600px">
  <tr><td align="center" style="padding:18px 20px 0 20px;font-family:Arial,Helvetica,sans-serif;font-size:11px;
    line-height:18px;color:#8f8676">{_e(brand_name or 'Newsletter')} &middot; {_e(issue_number)} &middot; {_e(date_str)}</td></tr>
</table>
</td></tr>
</table>
</div>"""
