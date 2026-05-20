"""LangGraph StateGraph for the document acquisition workflow.

Two graph variants are provided:

- **discover graph** (used by ``discover --engine langgraph``):
      discover → followup → rank → END
  Stops after ranking; does *not* run ``review_prep``.  The ``discover``
  command only writes discovery artefacts, so running ``review_prep`` would
  be wasted work and would emit misleading review-stage telemetry.

- **review graph** (used internally when a full pipeline is needed):
      discover → followup → rank → review_prep → END

Usage
-----
    from doc_workbench.orchestration.graph import run_graph

    final_state = run_graph(
        entities=entities,
        policy=policy,
        tracer=tracer,
        output_dir=output_dir,
        followup_search=True,
    )
    # final_state["ranked_records"] → list[DiscoveryRecord]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from langgraph.graph import END, StateGraph
except ImportError as _err:  # pragma: no cover
    raise ImportError(
        "The LangGraph orchestration path requires the 'langgraph' package.\n"
        "Install it with:  pip install 'doc_workbench[orchestration]'"
    ) from _err

from doc_workbench.observability.tracer import RunTrace
from doc_workbench.orchestration.nodes import (
    discover_node,
    followup_node,
    rank_node,
    review_prep_node,
    parse_node,
    extract_node,
    chunk_node,
)
from doc_workbench.orchestration.state import WorkbenchState
from doc_workbench.policy import ContextPolicy


def _build_discover_graph() -> Any:
    """Compile the discover-only graph: discover → followup → rank → END.

    Does not include ``review_prep`` — the ``discover`` command only writes
    discovery artefacts and should not emit review-stage spans.
    """
    builder: StateGraph = StateGraph(WorkbenchState)

    builder.add_node("discover", discover_node)
    builder.add_node("followup", followup_node)
    builder.add_node("rank", rank_node)

    builder.set_entry_point("discover")
    builder.add_edge("discover", "followup")
    builder.add_edge("followup", "rank")
    builder.add_edge("rank", END)

    return builder.compile()


def _build_graph() -> Any:
    """Compile the full acquisition StateGraph: discover → followup → rank → review_prep → END."""
    builder: StateGraph = StateGraph(WorkbenchState)

    builder.add_node("discover", discover_node)
    builder.add_node("followup", followup_node)
    builder.add_node("rank", rank_node)
    builder.add_node("review_prep", review_prep_node)

    builder.set_entry_point("discover")
    builder.add_edge("discover", "followup")
    builder.add_edge("followup", "rank")
    builder.add_edge("rank", "review_prep")
    builder.add_edge("review_prep", END)

    return builder.compile()


def run_graph(
    *,
    entities: list[Any],
    policy: ContextPolicy,
    tracer: RunTrace,
    output_dir: Path,
    followup_search: bool = False,
    mode: str = "discover",
    exec_policy: Any = None,
) -> WorkbenchState:
    """Run the acquisition graph and return the final state.

    Parameters
    ----------
    entities:
        List of EntityRecord objects to process.
    policy:
        Loaded ContextPolicy instance.
    tracer:
        RunTrace instance for local span recording.
    output_dir:
        Path to the run output directory (available to nodes for artefacts).
    followup_search:
        Whether to run the follow-up extraction stage.
    mode:
        ``"discover"`` (default) — stops after rank; no review_prep.
        ``"full"`` — runs all four nodes including review_prep.
    exec_policy:
        Optional ExecutionPolicy instance.  When provided, nodes enforce
        execution-policy rules (e.g. followup_search.enabled) before acting.

    Returns
    -------
    WorkbenchState with all stage outputs populated.
    """
    if mode == "full":
        graph = _build_graph()
    else:
        graph = _build_discover_graph()

    initial_state: WorkbenchState = {
        "entities": entities,
        "policy": policy,
        "exec_policy": exec_policy,
        "tracer": tracer,
        "output_dir": output_dir,
        "followup_search": followup_search,
    }

    return graph.invoke(initial_state)


def build_intake_graph() -> Any:
    """Compile the intake StateGraph: parse → extract → chunk → END.

    State must contain at least one of:
      - intake_document_ids  (list of document ID strings)
      - intake_entity_id     (string entity ID)
      - intake_all           (bool True)

    And the following paths:
      - intake_registry_root (Path)

    Optional:
      - intake_force (bool, default False)

    Returns the compiled graph.
    """
    builder: StateGraph = StateGraph(WorkbenchState)

    builder.add_node("parse", parse_node)
    builder.add_node("extract", extract_node)
    builder.add_node("chunk", chunk_node)

    builder.set_entry_point("parse")
    builder.add_edge("parse", "extract")
    builder.add_edge("extract", "chunk")
    builder.add_edge("chunk", END)

    return builder.compile()
