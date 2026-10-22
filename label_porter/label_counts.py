"""Bounded, read-only counts of rule-targeted label arrays; no rule re-evaluation."""
from collections import Counter

from .formats import MISSING, get_pointer


def _incoming(expr, parent):
    if isinstance(expr, str):
        return None if parent is None else parent + (expr,)
    if not isinstance(expr, dict) or expr.get("transforms") or "path" not in expr:
        return None
    scope = expr.get("scope", "result")
    if scope == "root_result":
        return (expr["path"],)
    return parent + (expr["path"],) if scope == "result" and parent is not None else None


def _measure(record, chain):
    counts = Counter(records=1)
    if chain is None:
        counts["unavailable"] = 1
        return counts

    def visit(value, depth):
        value = get_pointer(value, chain[depth])
        if value is MISSING:
            counts["missing"] += 1
        elif not isinstance(value, list):
            counts["non_array"] += 1
        elif depth == len(chain) - 1:
            counts["arrays"] += 1
            counts["items"] += len(value)
        else:
            for item in value:
                visit(item, depth + 1)

    visit(record, 0)
    return counts


class LabelCounts:
    """One row per target array. Nested arrays remain separate, never summed."""
    def __init__(self, config=None):
        self.targets = {}

        def add(chain, incoming, explicit):
            entry = self.targets.setdefault(chain, {"incoming": incoming, "explicit": False,
                "counts": {side: Counter() for side in
                           ("source", "result", "output", "output_before", "completed", "examine")}})
            if entry["incoming"] != incoming:
                entry["incoming"] = None  # Multiple rules cannot define one unambiguous input count.
            entry["explicit"] |= explicit

        def walk(rules, source=(), result=()):
            for rule in rules:
                chain = source + (rule["to"],)
                incoming = _incoming(rule["from"], result)
                is_list = rule["kind"] in ("items", "rebuild_items")
                if is_list or rule.get("type") == "array" or rule.get("allow_container_replace"):
                    add(chain, incoming, is_list or rule.get("type") == "array")
                if is_list:
                    walk(rule["rules"], chain, incoming)
                    for ref in rule.get("references", []):
                        add(source + (ref["path"],), None, True)

        walk((config or {}).get("rules", []))

    def add(self, side, record):
        for chain, entry in self.targets.items():
            entry["counts"][side].update(_measure(record, entry["incoming"] if side == "result" else chain))

    def rows(self):
        rows = []
        for chain, entry in self.targets.items():
            counts = entry["counts"]
            # Scalar fields and metadata are not labels merely because a rule exists.
            if not entry["explicit"] and not any(c["arrays"] for c in counts.values()):
                continue
            normalized = {side: {key: c[key] for key in
                ("records", "arrays", "items", "missing", "non_array", "unavailable")}
                for side, c in counts.items()}
            before, after = normalized["output_before"], normalized["output"]
            complete = all(not c[k] for c in (before, after) for k in ("missing", "non_array", "unavailable"))
            rows.append({"path": "[]".join(chain), "source_paths": list(chain),
                "result_path": "[]".join(entry["incoming"]) if entry["incoming"] is not None else None,
                "counts": normalized, "delta": after["items"] - before["items"] if complete else None})
        return rows
