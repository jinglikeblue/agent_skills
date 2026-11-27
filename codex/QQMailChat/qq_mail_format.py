"""Render common Markdown in a self-contained, mobile-friendly email body."""

import re
from html import escape
from urllib.parse import urlsplit


INLINE = re.compile(r"`([^`\n]+)`|(?<!!)\[([^\]\n]+)\]\(([^()\s]+)\)|\*\*([^*\n]+)\*\*")
HEADING = re.compile(r"(#{1,6})\s+(.+)")
LIST_ITEM = re.compile(r"([-*+]|\d+[.)])\s+(.+)")
FENCE = re.compile(r"(`{3,}|~{3,}).*")
TABLE_DIVIDER = re.compile(r":?-{3,}:?")
PARAGRAPH = 'style="margin:0 0 12px"'


def inline_html(text):
    # Escape every source fragment. Only these explicit link schemes produce anchors.
    output = []
    start = 0
    for match in INLINE.finditer(text):
        output.append(escape(text[start:match.start()]))
        code, label, url, bold = match.groups()
        if code is not None:
            output.append(f'<code style="background:#eef2f6;overflow-wrap:anywhere">{escape(code)}</code>')
        elif bold is not None:
            output.append(f"<strong>{escape(bold)}</strong>")
        else:
            try:
                scheme = urlsplit(url).scheme
            except ValueError:
                scheme = ""
            if scheme in {"https", "http", "mailto", "codex"}:
                output.append(f'<a href="{escape(url, quote=True)}" style="color:#1755a5">{escape(label)}</a>')
            else:
                output.append(escape(match[0]))
        start = match.end()
    output.append(escape(text[start:]))
    return "".join(output)


def table_cells(line):
    row = line.strip().removeprefix("|").removesuffix("|")
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", row)]


def table_html(headers, rows):
    style = "padding:6px;border:1px solid #dbe2ea;text-align:left;vertical-align:top;overflow-wrap:anywhere"
    head = "".join(f'<th scope="col" style="{style};background:#eef2f6">{inline_html(cell)}</th>' for cell in headers)
    body = ["<tr>" + "".join(f'<td style="{style}">{inline_html(cell)}</td>' for cell in row) + "</tr>" for row in rows]
    return (
        '<table style="width:100%;table-layout:fixed;border-collapse:collapse;margin:0 0 12px;'
        'font-size:15px;overflow-wrap:anywhere">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def markdown_html(text):
    lines = text.splitlines()
    blocks = []
    paragraph = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        block = ""
        fence = FENCE.fullmatch(line)
        heading = HEADING.fullmatch(line)
        item = LIST_ITEM.fullmatch(line)
        if fence:
            delimiter = fence[1]
            code = []
            while index < len(lines):
                source = lines[index]
                index += 1
                if source.strip().startswith(delimiter) and set(source.strip()) == {delimiter[0]}:
                    break
                code.append(source)
            rendered = escape("\n".join(code))
            block = ('<pre style="margin:0 0 12px;padding:10px;background:#eef2f6;'
                     f'white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px"><code>{rendered}</code></pre>')
        elif heading or line in {"问题", "结论", "原因"}:
            level = len(heading[1]) if heading else 2
            caption = inline_html(heading[2] if heading else line)
            size = 22 if level == 1 else 17
            block = f'<h{level} style="margin:16px 0 8px;font-size:{size}px;line-height:1.3">{caption}</h{level}>'
        elif item:
            ordered = item[1][0].isdigit()
            tag = "ol" if ordered else "ul"
            items = [item[2]]
            while index < len(lines):
                following = LIST_ITEM.fullmatch(lines[index].strip())
                if not following or following[1][0].isdigit() != ordered:
                    break
                items.append(following[2])
                index += 1
            entries = "".join(f'<li style="margin:0 0 4px">{inline_html(content)}</li>' for content in items)
            block = f'<{tag} style="margin:0 0 12px;padding-left:24px">{entries}</{tag}>'
        elif line.startswith(">"):
            quoted = [line[1:].lstrip()]
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quoted.append(lines[index].lstrip()[1:].lstrip())
                index += 1
            content = "<br>".join(inline_html(part) for part in quoted)
            block = f'<blockquote style="margin:0 0 12px;padding-left:10px;border-left:3px solid #cbd5e1">{content}</blockquote>'
        elif line:
            headers = table_cells(line) if "|" in line else []
            divider = table_cells(lines[index]) if headers and index < len(lines) else []
            if len(headers) < 2 or len(divider) != len(headers) or not all(map(TABLE_DIVIDER.fullmatch, divider)):
                paragraph.append(line)
                continue
            index += 1
            rows = []
            while index < len(lines) and "|" in lines[index]:
                cells = table_cells(lines[index])
                if len(cells) != len(headers):
                    break
                rows.append(cells)
                index += 1
            block = table_html(headers, rows)

        if paragraph:
            blocks.append(f'<p {PARAGRAPH}>{"<br>".join(map(inline_html, paragraph))}</p>')
            paragraph.clear()
        if block:
            blocks.append(block)
    if paragraph:
        blocks.append(f'<p {PARAGRAPH}>{"<br>".join(map(inline_html, paragraph))}</p>')
    return "\n".join(blocks)


def email_html(text):
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;color:#172434;background:#fff;-webkit-text-size-adjust:100%;'
        'font-family:-apple-system,BlinkMacSystemFont,Microsoft YaHei,sans-serif;font-size:16px;line-height:1.6">'
        '<div style="max-width:680px;margin:0 auto;padding:12px 16px;overflow-wrap:anywhere">'
        f"{markdown_html(text)}</div></body></html>"
    )
