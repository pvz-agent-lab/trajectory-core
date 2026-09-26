"""Byte-faithful legacy readers for the ``lvz.*`` evidence formats.

Every module in this package is a reduced adaptation of a file from
``guajun/llm-vs-zombies`` at commit ``11917a7``.  See ``NOTICES.md`` for the
per-file mapping and the exact changes.  The public package only uses this
closure to read, validate and package existing evidence; game semantics stay
here and are not re-exported as new concepts.
"""
