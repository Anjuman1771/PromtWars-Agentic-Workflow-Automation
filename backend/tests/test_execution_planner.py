"""Tests for deterministic, non-executing workflow planning."""

import pytest

from backend.app.core.planner import ExecutionPlanner
from backend.app.safety.exceptions import InvalidWorkflowSchemaError
from backend.app.safety.limits import ExecutionLimits


def planning_workflow():
    return {
        "tasks": [
            {"id": "d", "name": "D", "tool": "transform", "dependencies": ["a"]},
            {"id": "b", "name": "B", "tool": "transform"},
            {"id": "e", "name": "E", "tool": "transform", "dependencies": ["b"]},
            {"id": "a", "name": "A", "tool": "transform"},
            {"id": "c", "name": "C", "tool": "transform", "dependencies": ["a"]},
        ]
    }


def test_planning_order_is_deterministic_and_topological():
    planner = ExecutionPlanner(limits=ExecutionLimits(max_concurrent_tasks=10))
    first = planner.plan(planning_workflow())
    second = planner.plan(planning_workflow())

    assert first == second
    positions = {task_id: index for index, task_id in enumerate(first.topological_order)}
    for task in planning_workflow()["tasks"]:
        for dependency in task.get("dependencies", []):
            assert positions[dependency] < positions[task["id"]]


def test_independent_tasks_are_grouped_as_parallel_ready():
    planner = ExecutionPlanner(limits=ExecutionLimits(max_concurrent_tasks=10))
    plan = planner.plan(planning_workflow())

    assert plan.initial_ready_tasks == ["a", "b"]
    assert plan.parallel_groups == [["a", "b"], ["c", "d", "e"]]


def test_parallel_groups_respect_configured_concurrency_limit():
    planner = ExecutionPlanner(limits=ExecutionLimits(max_concurrent_tasks=2))
    plan = planner.plan(planning_workflow())

    assert all(len(group) <= 2 for group in plan.parallel_groups)
    assert plan.parallel_groups == [["a", "b"], ["c", "d"], ["e"]]


def test_plan_reports_all_initially_ready_tasks_when_batches_are_capped():
    planner = ExecutionPlanner(limits=ExecutionLimits(max_concurrent_tasks=2))
    plan = planner.plan(
        {
            "tasks": [
                {"id": task_id, "name": task_id, "tool": "transform"}
                for task_id in ("c", "a", "b")
            ]
        }
    )

    assert plan.initial_ready_tasks == ["a", "b", "c"]
    assert plan.parallel_groups == [["a", "b"], ["c"]]


def test_ready_tasks_follow_completed_dependencies():
    planner = ExecutionPlanner()

    assert planner.get_ready_tasks(planning_workflow()) == ["a", "b"]
    assert planner.get_ready_tasks(planning_workflow(), {"a"}) == ["b", "c", "d"]
    assert planner.get_ready_tasks(planning_workflow(), {"a", "b"}) == ["c", "d", "e"]


def test_unknown_completed_task_is_rejected():
    with pytest.raises(InvalidWorkflowSchemaError, match="Completed task IDs"):
        ExecutionPlanner().get_ready_tasks(planning_workflow(), {"unknown"})


def test_planner_wraps_invalid_input_as_domain_error():
    with pytest.raises(InvalidWorkflowSchemaError):
        ExecutionPlanner().plan({"tasks": [{"id": "missing-fields"}]})
