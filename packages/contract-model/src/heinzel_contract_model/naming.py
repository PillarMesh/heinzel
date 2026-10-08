from __future__ import annotations

__all__ = ["display_label"]


def display_label(output_name: str) -> str:
    """How a governed output name reads to a person.

    Output names are identifiers a contract approved, so they are lower-case and underscored
    wherever they appear. Every surface that shows one to a person has to turn it back into
    words, and two surfaces that do it differently show the same governed metric under two
    names -- which reads as two metrics. This is the one derivation, so the console's result
    table and a published dashboard's legend agree.

    Derived rather than stored: a display name a person could set would be a second place the
    name of a metric lives, approved by whoever approves a binding rather than by whoever owns
    the meaning. If the product ever wants names that are authored, they belong beside the
    semantic object that already carries one, and this is the fallback for a name it has not.
    """
    return output_name.replace("_", " ").strip().title()
