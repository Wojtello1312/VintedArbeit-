"""Read explicit item attributes from rendered public markup, never page scripts."""

from html.parser import HTMLParser

from vinted_deal_hunter.models import clean_text


class ItemAttributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.stack: list[tuple[str, str | None]] = []
        self.parts: dict[str, list[str]] = {}
        self.breadcrumbs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        testid = attributes.get("data-testid", "") or ""
        field = {
            "item-attributes-status": "condition",
            "item-attributes-size": "size",
            "item-attributes-brand": "brand",
            "item-attributes-category": "category",
        }.get(testid)
        if (
            "breadcrumb" in testid.casefold()
            or "breadcrumb" in (attributes.get("aria-label") or "").casefold()
        ):
            field = "breadcrumbs"
        if tag == "a" and any(key == "breadcrumbs" for _, key in self.stack):
            field = "breadcrumb_item"
        if field:
            self.parts.setdefault(field, [])
        if tag not in {
            "br",
            "hr",
            "img",
            "input",
            "meta",
            "link",
            "source",
            "wbr",
            "area",
            "embed",
        }:
            self.stack.append((tag, field))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                for _, field in self.stack[index:]:
                    if field:
                        value = clean_text(" ".join(self.parts.pop(field, [])))
                        if field == "breadcrumb_item":
                            if value:
                                self.breadcrumbs.append(value)
                            continue
                        if field == "breadcrumbs":
                            if self.breadcrumbs:
                                self.values.setdefault("category", " > ".join(self.breadcrumbs))
                            continue
                        labels = {
                            "condition": "Stan",
                            "brand": "Marka",
                            "size": "Rozmiar",
                            "category": "Kategoria",
                        }
                        label = labels[field]
                        if value.casefold().startswith(
                            (label.casefold() + " ", label.casefold() + ":")
                        ):
                            value = value[len(label) :].strip(" :")
                        if value:
                            self.values.setdefault(field, value)
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if any(tag in {"script", "style"} for tag, _ in self.stack):
            return
        for _, field in self.stack:
            if field in self.parts:
                self.parts[field].append(data)
