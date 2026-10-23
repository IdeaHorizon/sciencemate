"""Identify journal notices and publication metadata without excluding research topics."""
import re

_JOURNAL_NOTICE_TITLE = re.compile(
    r"^(?:corrigendum|errat(?:um|a)|retraction(?:\s+notice|\s+note)?|"
    r"retracted(?:\s+article)?|editor(?:'s|ial)?\s+note|editorial\s+board|masthead|"
    r"expression\s+of\s+concern|"
    r"withdrawal(?:\s+notice)?|correction\s+to|publisher\s+correction|"
    r"addendum\s+to)\b",
    re.I,
)


# Standalone publication metadata labels; a research title merely containing
# "issue", "information" or "contents" is not a notice.
_JOURNAL_FRONT_MATTER_TITLE = re.compile(
    r"^(?:issue\s+information|(?:table\s+of\s+)?contents|"
    r"front\s*matter|back\s*matter|(?:front|back)\s+cover(?:\s+image)?|"
    r"cover\s+image|associate\s+registry|author\s+index|subject\s+index|information\s+for\s+(?:authors|subscribers)|"
    r"instructions\s+(?:for|to)\s+authors|copyright(?:\s+information)?)"
    r"(?:\s*[:：,;–—-]\s*.*|\s+(?:vol(?:ume)?\.?|issue|no\.)\s+.*)?[.\s]*$",
    re.I,
)


_JOURNAL_ISSUE_GUIDE = re.compile(
    r"^insights?\s+into\s+the\s+(?:january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\s+20\d{2}\s+issue(?:\s+of\b.*)?[.\s]*$", re.I,
)


def is_journal_notice_title(title: str) -> bool:
    title = " ".join(str(title or "").split()).replace("’", "'").replace("‘", "'")
    return bool(_JOURNAL_NOTICE_TITLE.match(title)
                or _JOURNAL_FRONT_MATTER_TITLE.fullmatch(title)
                or _JOURNAL_ISSUE_GUIDE.fullmatch(title))
