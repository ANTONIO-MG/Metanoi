"""Importing a module's teaching content from a declarative JSON pack.

A *content pack* is one topic's worth of material — the study guide, the topic
mock and its solution, the week's slice of the integrated challenge — expressed
as data rather than as a sequence of admin clicks. :func:`validate` checks a
pack against the contract; :func:`import_pack` turns a valid one into database
rows.

The pack format is the *only* interface: :mod:`core.content_pack.drafting`
produces a pack for the same :func:`import_pack`, so whatever a person can review
as JSON is exactly what reaches the database::

    from core import content_pack
    text, metas, errors = content_pack.extract_many(files)     # local, no model
    pack, problems = content_pack.draft_pack(text, module=...) # Claude → JSON
    result = content_pack.import_pack(pack, actor=request.user)
"""

from .drafting import DraftingError, draft_pack, as_json     # noqa: F401
from .extract import ExtractionError, extract, extract_many  # noqa: F401
from .importer import (ImportResult, PackImportError, import_bundle, import_pack,  # noqa: F401
                       publish_imported)
from .schema import (BUNDLE_VERSION, PACK_VERSION, ValidationError, packs_of,  # noqa: F401
                     validate, validate_bundle)
