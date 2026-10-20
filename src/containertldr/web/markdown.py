import re

from markupsafe import Markup, escape

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def markdown(text: str) -> Markup:
    """The bit of Markdown summaries use, bold and lists, as HTML.

    Each line is escaped before the tags go in, so release notes and the model can't add HTML of their own.
    """
    html, in_list = [], False
    for line in text.splitlines():
        line = line.strip()
        item = line.startswith(("- ", "* "))
        if item != in_list:
            html.append("<ul>" if item else "</ul>")
            in_list = item
        if line:
            inline = _BOLD.sub(r"<strong>\1</strong>", str(escape(line[2:] if item else line)))
            html.append(f"<li>{inline}</li>" if item else f"<p>{inline}</p>")
    if in_list:
        html.append("</ul>")
    return Markup("\n".join(html))
