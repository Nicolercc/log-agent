"""
transitions.py -- the constitution of the system.

This table is the last line of defense against a confidently wrong classifier.
A model that returns {"kind": "onsite", "confidence": 0.97} for an application
already marked rejected is not uncertain. It is certain and wrong, and no
confidence threshold catches that. Only a legality check does.

OWNED BY THE HUMAN. Agents must not add entries. If a transition is missing,
that is a decision for a person to make, and the correct behavior in the
meantime is to route the proposal to the review queue.
"""

# from_status -> set of event kinds legally appendable next
TRANSITIONS: dict[str, set[str]] = {
    "applied": {"confirmed", "response", "screen", "rejected", "ghosted", "withdrawn"},
    "confirmed": {"response", "screen", "rejected", "ghosted", "withdrawn"},
    "response": {"screen", "tech", "rejected", "withdrawn"},
    "screen": {"tech", "onsite", "rejected", "withdrawn"},
    "tech": {"onsite", "final", "rejected", "withdrawn"},
    "onsite": {"final", "offer", "rejected", "withdrawn"},
    "final": {"offer", "rejected", "withdrawn"},
    "offer": {"accepted", "rejected", "withdrawn"},

    # Terminal. Nothing follows. Not "unlikely" -- forbidden.
    "rejected": set(),
    "accepted": set(),
    "withdrawn": set(),
    "ghosted": set(),
}

# Touch events do not advance stage and are legal from any non-terminal state.
# `note` is deliberately excluded from classifier output: a note is a human act,
# and letting a model write freeform notes reintroduces the unvalidated write path
# this whole design exists to remove.
ALWAYS_LEGAL_IF_OPEN = {"followup_sent", "thankyou_sent"}

TERMINAL_STATES = {"rejected", "accepted", "withdrawn", "ghosted"}


def is_legal(from_status: str, to_kind: str) -> tuple[bool, str]:
    """Return (allowed, reason) for a proposed state transition."""
    if from_status not in TRANSITIONS:
        return False, f"unknown current status {from_status!r}"
    if from_status in TERMINAL_STATES:
        return False, (
            f"{from_status} is terminal; refusing to append "
            f"{to_kind!r} to a closed application"
        )
    if to_kind in ALWAYS_LEGAL_IF_OPEN:
        return True, ""
    if to_kind in TRANSITIONS[from_status]:
        return True, ""
    return False, f"illegal transition {from_status} -> {to_kind}"
