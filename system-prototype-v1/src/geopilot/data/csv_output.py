"""Standard CSV readers for GeoPilot's result tables."""

from csv import DictReader, Error, field_size_limit, reader


__all__ = ["DictReader", "Error", "reader"]
MINIMUM_FIELD_CHARACTERS = 8 * 1024 * 1024

# The standard reader shares this limit across the interpreter. Set it once
# for result tables, whose provenance fields can contain thousands of IDs.
field_size_limit(max(field_size_limit(), MINIMUM_FIELD_CHARACTERS))
