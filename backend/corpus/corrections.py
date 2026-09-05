"""A person editing the graph the agent just wrote.

The most expensive training signal there is - someone looked at the model's output on a
real artifact and changed it - and it was already sitting in `graph_versions.author`,
needing only to be read. An `agent`-authored version followed by a `user`-authored one on
the same graph *is* a correction pair.

The whole difficulty is telling a correction from noise. Opening a policy makes the editor
normalise it, and dragging a node to see it better writes a version too. Neither is a
person disagreeing with the model, and training on them would teach the model that its
output is wrong when it was fine. So the diff here is deliberately behavioural: a change
that only moves nodes around is not a correction.
"""

from __future__ import annotations

from typing import Any

# Node keys that do not change what a policy decides.
_COSMETIC = {"position", "positionAbsolute", "width", "height", "selected", "dragging"}


def _nodes(graph: Any) -> dict[str, dict]:
    if not isinstance(graph, dict):
        return {}
    return {n["id"]: n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")}


def _edges(graph: Any) -> set[tuple]:
    if not isinstance(graph, dict):
        return set()
    return {
        (e.get("sourceId"), e.get("targetId"))
        for e in graph.get("edges") or []
        if isinstance(e, dict)
    }


def _behaviour(node: dict) -> dict:
    """The node stripped of everything that only affects how it looks."""
    return {k: v for k, v in node.items() if k not in _COSMETIC}


def describe_change(before: Any, after: Any) -> dict | None:
    """What changed between two versions, or None if nothing behavioural did.

    Returning None is the important half: it is what stops a layout tweak or the editor's
    own mount-time normalisation being recorded as a person correcting the model.
    """
    old_nodes, new_nodes = _nodes(before), _nodes(after)
    old_edges, new_edges = _edges(before), _edges(after)

    added = sorted(set(new_nodes) - set(old_nodes))
    removed = sorted(set(old_nodes) - set(new_nodes))
    changed = [
        node_id for node_id in sorted(set(old_nodes) & set(new_nodes))
        if _behaviour(old_nodes[node_id]) != _behaviour(new_nodes[node_id])
    ]
    edges_added = sorted(new_edges - old_edges)
    edges_removed = sorted(old_edges - new_edges)

    if not (added or removed or changed or edges_added or edges_removed):
        return None

    return {
        "nodes_added": [_label(new_nodes[i]) for i in added],
        "nodes_removed": [_label(old_nodes[i]) for i in removed],
        "nodes_changed": [
            {
                "id": node_id,
                "name": new_nodes[node_id].get("name"),
                "type": new_nodes[node_id].get("type"),
                "fields": _changed_fields(old_nodes[node_id], new_nodes[node_id]),
            }
            for node_id in changed
        ],
        "edges_added": len(edges_added),
        "edges_removed": len(edges_removed),
    }


def _label(node: dict) -> dict:
    return {"id": node.get("id"), "name": node.get("name"), "type": node.get("type")}


def _changed_fields(old: dict, new: dict) -> list[str]:
    keys = (set(old) | set(new)) - _COSMETIC
    return sorted(k for k in keys if old.get(k) != new.get(k))
