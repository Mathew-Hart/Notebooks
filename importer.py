"""Convert a OneNote export into editable native page objects."""

import base64
from collections import Counter
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import struct
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QImage, QTextDocument


class NotebookImportError(ValueError):
    """A notebook could not be read or converted."""


# Transparent placeholders keep the line height of independently movable
# objects that were embedded in a OneNote text outline.
TRANSPARENT_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACXBIWXMAAA9h"
    "AAAPYQGoP6dpAAAAC0lEQVQImWNgAAIAAAUAAWJVMogAAAAASUVORK5CYII="
)
RICH_TAGS = {
    "a", "b", "blockquote", "br", "cite", "code", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "s", "span", "strong",
    "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
}
RICH_STYLES = {
    "background", "background-color", "border", "color", "font-family", "font-size",
    "font-style", "font-weight", "list-style-type", "margin-bottom", "margin-left",
    "margin-right", "margin-top", "padding", "text-align", "text-decoration",
    "vertical-align", "width",
}
EQUATION = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]", re.DOTALL)


def _resolve_converter(explicit):
    search_path = os.pathsep.join([os.environ.get("PATH", ""), str(Path.home() / ".local/bin")])
    selected = explicit or os.environ.get("ONE2HTML") or "one2html"
    executable = shutil.which(selected, path=search_path)
    if not executable:
        raise NotebookImportError("Install one2html 2.0.1 and set ONE2HTML to its executable path.")
    return executable


def _soup(path):
    return BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")


def _relative_file(root, base, reference):
    try:
        address = urlsplit(reference)
    except ValueError:
        return None
    if address.scheme or address.netloc:
        return None
    # Image filenames from the converter may contain literal URL punctuation.
    for name in dict.fromkeys((unquote(address.path), reference)):
        path = (base / name).resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
    return None


def _styles(tag):
    return {
        key.strip().lower(): value.strip()
        for setting in tag.get("style", "").split(";")
        if ":" in setting
        for key, value in [setting.split(":", 1)]
    }


def _number(value, fallback=0.0):
    try:
        number = float(str(value).removesuffix("px"))
    except (ValueError, TypeError):
        return fallback
    return number if math.isfinite(number) else fallback


def _position(styles, fallback_y=30.0):
    return _number(styles.get("left"), 48.0), _number(styles.get("top"), fallback_y)


def _object(kind, x, y, w, h, **data):
    return {"type": kind, "x": x, "y": y, "w": w, "h": h, **data}


def _image(tag, page, root, x, y):
    path = _relative_file(root, page.parent, tag.get("src", ""))
    if path is None:
        return None
    data = path.read_bytes()
    image = QImage.fromData(data)
    if image.isNull():
        return None
    styles = _styles(tag)
    width, height = image.width(), image.height()
    scale = min(
        1.0,
        _number(styles.get("max-width"), width) / width,
        _number(styles.get("max-height"), height) / height,
    )
    return _object(
        "image", x, y, max(1, round(width * scale)), max(1, round(height * scale)),
        data=base64.b64encode(data).decode("ascii"),
    )


def _ink(svg, dx=0.0, dy=0.0):
    """Read the converter's absolute move and relative line commands by stroke."""
    for path in svg.find_all("path", recursive=False):
        width = max(1.0, _number(path.get("stroke-width"), 1.0))
        color = QColor(path.get("stroke", "").replace(" ", ""))
        if not color.isValid():
            rgb = re.fullmatch(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", path.get("stroke", ""))
            color = QColor(*(int(value) for value in rgb.groups())) if rgb else QColor("#263246")
        opacity = _number(path.get("opacity"), 1.0)
        color.setAlphaF(max(0.0, min(1.0, opacity)))
        for stroke in path.get("d", "").split("M")[1:]:
            move, _, lines = stroke.partition("l")
            coordinates = move.split()
            if len(coordinates) != 2:
                continue
            x, y = (_number(value) for value in coordinates)
            points = [[x, y]]
            values = iter(lines.split())
            for delta_x, delta_y in zip(values, values):
                x += _number(delta_x)
                y += _number(delta_y)
                points.append([x, y])
            left = min(p[0] for p in points)
            top = min(p[1] for p in points)
            yield _object(
                "ink", left + dx, top + dy,
                max(p[0] for p in points) - left, max(p[1] for p in points) - top,
                points=[[x - left, y - top] for x, y in points], width=width,
                color=color.name(QColor.NameFormat.HexArgb),
            )


def _placeholder(soup, width, height):
    return soup.new_tag("img", src=TRANSPARENT_IMAGE, width=str(width), height=str(height))


def _sanitize_rich_text(tag):
    for child in list(tag.find_all(True)):
        if child.parent is None:
            continue
        if child.name == "img":
            continue  # At this stage, only our generated placeholders remain.
        if child.name in {"script", "style", "embed", "audio", "video", "object", "svg", "iframe"}:
            child.decompose()
            continue
        if child.name not in RICH_TAGS:
            child.unwrap()
            continue
        attributes = {
            key: child[key]
            for key in ("colspan", "rowspan", "border", "cellpadding", "cellspacing", "start")
            if key in child.attrs and str(child[key]).isdigit()
        }
        styles = {
            key: value for key, value in _styles(child).items()
            if key in RICH_STYLES and not re.search(r"url\s*\(|expression\s*\(", value, re.I)
        }
        if styles:
            attributes["style"] = "; ".join(f"{key}: {value}" for key, value in styles.items())
        href = child.get("href", "")
        if child.name == "a" and re.match(r"(?:https?://|mailto:)", href, re.I):
            attributes["href"] = href
        child.attrs = attributes


def _inline_positions(document):
    """Yield placeholder offsets in the order used in the converted outline."""
    layout = document.documentLayout()
    layout.documentSize()
    block = document.begin()
    while block.isValid():
        origin = layout.blockBoundingRect(block).topLeft()
        for fragment in block:
            part = fragment.fragment()
            image = part.charFormat()
            if not image.isImageFormat():
                continue
            index = part.position() - block.position()
            line = block.layout().lineForTextPosition(index)
            if line.isValid():
                height = image.toImageFormat().height()
                yield (
                    origin.x() + line.cursorToX(index)[0],
                    origin.y() + line.y() + line.ascent() - height,
                )
        block = block.next()


def _outline(tag, page, root, warnings, x, y, width):
    """Lay out editable text and independent objects embedded in the outline."""
    soup = BeautifulSoup(str(tag), "html.parser")
    wrapper = soup.find(True)
    elements = []

    for child in list(wrapper.find_all(["img", "svg", "embed", "audio", "video"])):
        if child.name == "img":
            obj = _image(child, page, root, 0, 0)
            if obj is None:
                warnings["unreadable images"] += 1
                child.decompose()
                continue
            width_px, height_px, objects = obj["w"], obj["h"], [obj]
        elif child.name == "svg":
            if child.parent.name != "span" or "ink-text" not in child.parent.get("class", []):
                child.decompose()
                continue
            child = child.parent
            vector = child.svg
            dimensions = vector.get("viewbox", "").split()
            view_x, view_y = map(_number, dimensions[:2]) if len(dimensions) >= 2 else (0, 0)
            styles = _styles(child)
            width_px = max(1, _number(styles.get("width"), 20))
            height_px = max(1, _number(styles.get("height"), 20))
            objects = list(_ink(vector, -view_x, -view_y))
        else:
            warnings["embedded files or media skipped"] += 1
            child.replace_with("[Embedded file]")
            continue
        placeholder = _placeholder(soup, width_px, height_px)
        placeholder["data-index"] = str(len(elements))
        elements.append(objects)
        child.replace_with(placeholder)

    for node in list(wrapper.find_all(string=EQUATION)):
        pieces = []
        end = 0
        for match in EQUATION.finditer(str(node)):
            pieces.append(NavigableString(node[end:match.start()]))
            equation = match.group(1) or match.group(2) or ""
            width_px = max(100.0, min(420.0, len(equation) * 6.0))
            placeholder = _placeholder(soup, width_px, 32)
            placeholder["data-index"] = str(len(elements))
            elements.append([_object("equation", 0, 0, width_px, 32, source=equation)])
            pieces.append(placeholder)
            end = match.end()
        pieces.append(NavigableString(node[end:]))
        node.replace_with(*pieces)

    ordered = [elements[int(img.attrs.pop("data-index"))] for img in wrapper.find_all("img")]
    _sanitize_rich_text(wrapper)
    wrapper.attrs = {"style": "font-family: Calibri, Carlito, sans-serif; font-size: 6pt;"}
    markup = str(wrapper)

    doc = QTextDocument()
    if ordered:
        image = QImage(1, 1, QImage.Format.Format_ARGB32)
        image.fill(0)
        doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(TRANSPARENT_IMAGE), image)
    doc.setHtml(markup)
    doc.setTextWidth(width)
    height = max(24.0, doc.size().height())
    objects = []
    if wrapper.get_text(strip=True) or wrapper.find("table"):
        objects.append(_object("text", x, y, width, height, html=markup))
    positions = list(_inline_positions(doc))
    if len(positions) != len(ordered):
        warnings["inline objects with approximated placement"] += abs(len(positions) - len(ordered))
        positions.extend((4.0, height + index * 24.0) for index in range(len(ordered) - len(positions)))
    for group, (left, top) in zip(ordered, positions):
        for obj in group:
            obj["x"] += x + left
            obj["y"] += y + top
            objects.append(obj)
    return objects, height


def _page(path, title, root, warnings):
    soup = _soup(path)
    if not soup.find("meta", attrs={"name": "X-Original-Page-Id"}) or not soup.body:
        return None
    objects = []
    next_y = 30.0
    for tag in soup.body.find_all(recursive=False):
        styles = _styles(tag)
        x, y = _position(styles, next_y)
        if tag.name == "svg":
            objects.extend(_ink(tag))
        elif tag.name == "img":
            image = _image(tag, path, root, x, y)
            if image is None:
                warnings["unreadable images"] += 1
            else:
                objects.append(image)
                next_y = max(next_y, y + image["h"])
        elif tag.name == "div":
            width = _number(styles.get("width") or styles.get("max-width"), 460.0)
            layer, height = _outline(tag, path, root, warnings, x, y, max(100.0, width))
            objects.extend(layer)
            next_y = max(next_y, y + height)
        elif tag.name in {"embed", "audio", "video"}:
            warnings["embedded files or media skipped"] += 1
    return {"title": title, "objects": objects}


def _package_sections(source):
    """Read filenames from the CAB directory without extracting the package."""
    result = []
    with source.open("rb") as stream:
        header = stream.read(36)
        if len(header) < 36 or header[:4] != b"MSCF":
            return result
        file_offset = struct.unpack_from("<I", header, 16)[0]
        file_count = struct.unpack_from("<H", header, 28)[0]
        if not 36 <= file_offset < source.stat().st_size:
            return result
        stream.seek(file_offset)
        for _ in range(file_count):
            record = stream.read(16)
            if len(record) != 16:
                break
            filename = bytearray()
            for _ in range(4096):
                letter = stream.read(1)
                if not letter or letter == b"\x00":
                    break
                filename.extend(letter)
            else:
                break
            flags = struct.unpack_from("<H", record, 14)[0]
            encoding = "utf-8" if flags & 0x80 else "cp1252"
            path = PureWindowsPath(filename.decode(encoding, errors="replace"))
            if path.suffix.lower() == ".one":
                result.append(path)
    return result


def _notebook_index(path, root, source):
    soup = _soup(path)
    section_links = soup.select("nav a[target=section]")
    sections = []
    warnings = Counter()
    details = []
    groups = []
    if not section_links:
        rows = [(path, soup.title.get_text(strip=True), [])] if soup.title else []
    else:
        rows = []
        for entry in soup.select("nav li"):
            depth_match = re.search(r"margin-left:\s*(\d+)rem", entry.get("style", ""))
            depth = int(depth_match.group(1)) if depth_match else 0
            groups = groups[:depth]
            if "group" in entry.get("class", []):
                groups.append(entry.get_text(strip=True))
                continue
            link = entry.find("a", attrs={"target": "section"})
            if link is None:
                continue
            label = link.get("title") or link.get_text(strip=True)
            section_path = _relative_file(root, path.parent, link.get("href", ""))
            if section_path is None:
                details.append(f"Could not read the section {label}.")
            else:
                rows.append((section_path, label, groups[:]))

    for section, label, group in rows:
        pages = []
        for link in _soup(section).select("nav a[target=content]"):
            if "warnings" in link.get("class", []):
                details.append(f"The converter reported content warnings in {label}.")
                continue
            page_title = link.get("title") or link.get_text(strip=True) or "Untitled page"
            page_path = _relative_file(root, section.parent, link.get("href", ""))
            page = _page(page_path, page_title, root, warnings) if page_path else None
            if page is None:
                warnings["unreadable pages"] += 1
            else:
                pages.append(page)
        sections.append({"title": label, "path": group, "pages": pages})

    if source.suffix.lower() == ".onepkg":
        files = _package_sections(source)
        if len(files) > len(rows):
            titles = {tuple(part.casefold() for part in [*group, label]) for _, label, group in rows}
            unmatched = [
                file for file in files
                if tuple(part.casefold() for part in file.with_suffix("").parts) not in titles
            ]
            missing = len(files) - len(rows)
            explanation = f"{missing} of {len(files)} source section files were omitted by the converter"
            if len(unmatched) == missing:
                explanation += " (" + ", ".join(file.stem for file in unmatched) + ")"
            details.append(explanation + ".")
    details.extend(f"{count} {message}." for message, count in warnings.items())
    return {
        "title": source.stem,
        "sections": sections,
        "warnings": details,
    }


def import_notebook(source: Path, converter: str | None = None) -> dict:
    """Read a .onepkg, .onetoc2 or .one file using a local one2html executable."""
    source = source.resolve()
    if source.suffix.lower() not in {".onepkg", ".onetoc2", ".one"} or not source.is_file():
        raise NotebookImportError("Choose a readable .onepkg, .onetoc2 or .one file.")
    executable = _resolve_converter(converter)
    with TemporaryDirectory(prefix="notes-import-") as directory:
        root = Path(directory).resolve()
        try:
            process = subprocess.run(
                [executable, "--warnings", "--math-target", "latex", "--note-tag-icons", "emoji",
                 "-i", str(source), "-o", str(root)],
                capture_output=True, timeout=300, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise NotebookImportError("OneNote conversion timed out.") from error
        except OSError as error:
            raise NotebookImportError(f"Could not start one2html. {error.strerror}") from error
        if process.returncode:
            raise NotebookImportError("one2html could not convert the selected OneNote file.")
        indexes = sorted(root.glob("*.html"))
        if not indexes:
            raise NotebookImportError("The converter returned no notebook or section index.")
        result = _notebook_index(indexes[0], root, source)
        if not any(section["pages"] for section in result["sections"]):
            raise NotebookImportError("The converter returned no readable pages.")
        return result
