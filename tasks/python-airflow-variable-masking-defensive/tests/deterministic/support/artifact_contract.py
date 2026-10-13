"""Deterministic artifact formatting: equivalent Markdown is permitted."""
import re

REQUIRED_SECTIONS = (
    'Remediation Plan', 'Root causes identified', 'Production changes',
    'Preserved legitimate behavior', 'Validation',
)


def required_plan_sections(text: str) -> dict[str, bool]:
    headings = set()
    previous = ''
    fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(('```', '~~~')):
            fenced = not fenced
            previous = ''
            continue
        if fenced:
            continue
        match = re.match(r'^ {0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$', line)
        if match:
            heading = match.group(1)
        elif previous.strip() and re.fullmatch(r' {0,3}(?:=+|-+)[ \t]*', line):
            heading = previous.strip()
        else:
            previous = line
            continue
        heading = re.sub(r'[*_`]', '', heading).strip().casefold()
        headings.add(heading)
        previous = line
    return {name: name.casefold() in headings for name in REQUIRED_SECTIONS}
