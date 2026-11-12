#!/usr/bin/env python3
"""Refresh indexes/fields in a DOCX through a running LibreOffice UNO listener."""

from __future__ import annotations

import sys
from pathlib import Path

import uno
from com.sun.star.beans import PropertyValue


def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def main() -> None:
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: update_docx_fields.py /absolute/path/report.docx [port]")
    path = Path(sys.argv[1]).resolve()
    port = int(sys.argv[2]) if len(sys.argv) == 3 else 2002
    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_context
    )
    context = resolver.resolve(
        f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
    )
    service_manager = context.ServiceManager
    desktop = service_manager.createInstanceWithContext("com.sun.star.frame.Desktop", context)
    url = uno.systemPathToFileUrl(str(path))
    # DOCX round-tripping can change TOC line spacing on first import. Reopen
    # the stored file once so the cached numbers use the delivered layout.
    for _ in range(2):
        document = desktop.loadComponentFromURL(url, "_blank", 0, (prop("Hidden", True),))
        if document is None:
            raise RuntimeError(f"LibreOffice could not open {path}")
        try:
            previous = None
            for _ in range(6):
                document.refresh()
                document.reformat()
                indexes = document.getDocumentIndexes()
                for idx in range(indexes.getCount()):
                    indexes.getByIndex(idx).update()
                document.getTextFields().refresh()
                document.reformat()
                current = tuple(indexes.getByIndex(idx).getAnchor().getString()
                                for idx in range(indexes.getCount()))
                if current == previous:
                    break
                previous = current
            else:
                raise RuntimeError("TOC pagination did not stabilize after six updates")
            document.store()
        finally:
            document.close(True)


if __name__ == "__main__":
    main()
