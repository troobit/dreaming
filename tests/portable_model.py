"""A scripted model for the portable end-to-end test (test_portable.py).

The fake `claude` loads this file through its `responder` scenario key, so it
runs in the fake's own process and imports nothing from dreaming. The reader
quotes every user entry, keyed by the text after `key:`, under the
help-requests lens when the key is about learning and the preferences lens
otherwise. The reviewer creates one preference per preferences cluster,
leaving the scope to the program (Q100), and makes each help-requests cluster
a learning candidate with a research question. The researcher finds nothing,
and the author recommends a next step. A new cluster's key is its
`<lens>/<key>` reference.
"""

import json

START, END = "<dreaming-input>", "</dreaming-input>"
REVIEW = {
    "assignments": [],
    "pattern_merges": [],
    "knowledge": [],
    "item_merges": [],
    "candidates": [],
    "questions": [],
}


def _data(prompt):
    start = prompt.index(START) + len(START)
    return json.loads(prompt[start : prompt.rindex(END)])


def reader(data):
    found = []
    for entry in data["entries"]:
        if entry["role"] != "user":
            continue
        text = entry["text"]
        key = text.split("key:", 1)[1].strip()
        found.append(
            {
                "entry_id": entry["id"],
                "quote": text,
                "kind": "user_statement",
                "modality": "asserted",
                "outcome": "unknown",
                "outcome_entry_id": None,
                "lens": "help-requests" if "learn" in key else "preferences",
                "pattern_key": key,
            }
        )
    return {"observations": found}


def _ids(evidence):
    return [found["id"] for found in evidence]


def reviewer(data):
    clusters = data["new_clusters"]
    learning = [c for c in clusters if c["key"].startswith("help-requests/")]
    candidates = [
        {
            "pattern": cluster["key"],
            "proposal": "learning",
            "repo": None,
            "summary": "Learn what the user asked about",
            "evidence": _ids(cluster["evidence"]),
            "reason": "the user stated a learning goal",
        }
        for cluster in learning
    ]
    questions = [
        {"question": "What is a good first resource?", "candidate": n, "reason": ""}
        for n in range(len(candidates))
    ]
    created = [
        {
            "op": "create",
            "item_id": None,
            "base_version": None,
            "kind": "preference",
            "content": f"The user {cluster['key'].split('/', 1)[1]}.",
            "scope": {
                "domains": [],
                "projects": [],
                "workflows": [],
                "global": False,
                "unresolved": False,
            },
            "confidence": "medium",
            "confidence_basis": "stated by the user",
            "supporting": _ids(cluster["evidence"]),
            "contradicting": [],
            "pattern": cluster["key"],
            "reason": "an explicit preference",
        }
        for cluster in clusters
        if cluster not in learning
    ]
    return {
        **REVIEW,
        "knowledge": created,
        "candidates": candidates,
        "questions": questions,
    }


def researcher(data):
    return {"findings": [], "unavailable": []}


def author(data):
    return {
        "verification": {"supported": True, "reason": "the user said so"},
        "outcome": "learning",
        "changes": [],
        "learning": [
            {
                "summary": "Learn how Rust lifetimes work",
                "next_step": "Work through the lifetimes chapter of the Rust book",
                "helps_achieve": "Fewer stalls on borrow checker errors",
                "scope": {
                    "domains": [],
                    "projects": [],
                    "workflows": [],
                    "global": False,
                    "unresolved": False,
                },
                "evidence": _ids(data["evidence"]),
                "research": [],
            }
        ],
        "deferred_steps": [],
    }


ROLES = {
    "observations": reader,
    "assignments": reviewer,
    "findings": researcher,
    "verification": author,
}


def answer(prompt, schema):
    """A role's answer, or None for a prompt without dreaming input (a probe)."""
    if START not in prompt:
        return None
    wanted = schema["properties"]
    for field, role in ROLES.items():
        if field in wanted:
            return role(_data(prompt))
    raise ValueError(f"no scripted answer for a schema with {sorted(wanted)}")
