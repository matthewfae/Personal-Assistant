"""Tests for the db/ CRUD layer: tasks, shopping, symptoms, context bound and the mutation log."""

import json

import pytest

from db import context, shopping, symptoms, tasks
from db.connection import get_db


def _mutations():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT table_name, operation, record_id, data_json FROM mutation_log ORDER BY id"
        ).fetchall()
    return [(r["table_name"], r["operation"], r["record_id"], json.loads(r["data_json"])) for r in rows]


def _write_event(type_):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO events (timestamp, turn_id, type, payload) VALUES ('t', 't', ?, '{}')", (type_,)
        )
        return cur.lastrowid


# --- tasks ---

def test_add_and_get_task():
    task = tasks.add_task("home", "fix sink", priority=2)
    assert tasks.get_task(task["id"]) == task
    assert task["status"] == "open"
    assert tasks.get_task(999) is None


def test_update_task_changes_only_given_fields():
    task = tasks.add_task("home", "fix sink", priority=2)
    updated = tasks.update_task(task["id"], status="done", bogus="ignored")
    assert (updated["status"], updated["priority"], updated["summary"]) == ("done", 2, "fix sink")
    assert updated["updated_at"] >= task["updated_at"]


def test_update_task_errors():
    with pytest.raises(ValueError, match="not found"):
        tasks.update_task(999, status="done")
    task = tasks.add_task("home", "fix sink")
    with pytest.raises(ValueError, match="No valid fields"):
        tasks.update_task(task["id"], bogus=1)


def test_list_tasks_filters_and_order():
    low = tasks.add_task("home", "low", priority=3)
    none = tasks.add_task("work", "unprioritized")
    high = tasks.add_task("home", "high", priority=1)
    done = tasks.add_task("home", "done already", priority=1, status="done")

    def ids(**kw):
        return [t["id"] for t in tasks.list_tasks(**kw)]

    # Default is open only; priority ascending, NULL priority last.
    assert ids() == [high["id"], low["id"], none["id"]]
    assert ids(status="all") == [high["id"], done["id"], low["id"], none["id"]]
    assert ids(status="done") == [done["id"]]
    assert ids(area="work") == [none["id"]]
    assert ids(search="hig") == [high["id"]]


def test_task_writes_are_logged():
    task = tasks.add_task("home", "fix sink")
    tasks.update_task(task["id"], status="done")
    log = _mutations()
    assert [(t, op, rid) for t, op, rid, _ in log] == [("tasks", "INSERT", task["id"]), ("tasks", "UPDATE", task["id"])]
    assert log[1][3]["status"] == "done"


# --- shopping ---

def test_shopping_add_list_remove():
    milk = shopping.add_shopping("milk")
    eggs = shopping.add_shopping("eggs")
    assert shopping.list_shopping() == [{"id": milk["id"], "item": "milk"}, {"id": eggs["id"], "item": "eggs"}]
    assert shopping.remove_shopping(milk["id"]) is True
    assert shopping.remove_shopping(milk["id"]) is False
    assert [i["item"] for i in shopping.list_shopping()] == ["eggs"]
    # A failed remove writes no log entry.
    assert [op for _, op, _, _ in _mutations()] == ["INSERT", "INSERT", "DELETE"]


def test_shopping_task_id_must_exist():
    task = tasks.add_task("home", "bake")
    assert shopping.add_shopping("flour", task_id=task["id"])["task_id"] == task["id"]
    with pytest.raises(Exception, match="FOREIGN KEY"):
        shopping.add_shopping("sugar", task_id=999)


# --- symptoms ---

def test_log_and_get_symptom():
    s = symptoms.log_symptom("headache", severity=4, notes="after lunch")
    assert symptoms.get_symptom(s["id"]) == s
    # occurred_at defaults to logged_at.
    assert s["occurred_at"] == s["logged_at"]
    assert symptoms.get_symptom(999) is None


def test_update_symptom():
    s = symptoms.log_symptom("headache", severity=4)
    assert symptoms.update_symptom(s["id"], severity=2)["severity"] == 2
    with pytest.raises(ValueError, match="not found"):
        symptoms.update_symptom(999, severity=2)
    with pytest.raises(ValueError, match="No valid fields"):
        symptoms.update_symptom(s["id"], logged_at="x")


def test_list_symptoms_filters_and_order():
    old = symptoms.log_symptom("Headache", occurred_at="2026-10-01T09:00:00+00:00")
    new = symptoms.log_symptom("headache", occurred_at="2026-10-20T09:00:00+00:00")
    other = symptoms.log_symptom("nausea", occurred_at="2026-10-10T09:00:00+00:00")

    def ids(**kw):
        return [s["id"] for s in symptoms.list_symptoms(**kw)]

    assert ids() == [new["id"], other["id"], old["id"]]  # newest first
    assert ids(symptom="HEAD") == [new["id"], old["id"]]  # case-insensitive substring
    assert ids(since="2026-10-10T09:00:00+00:00") == [new["id"], other["id"]]  # inclusive
    assert ids(limit=1) == [new["id"]]


def test_delete_symptom():
    s = symptoms.log_symptom("headache")
    assert symptoms.delete_symptom(s["id"]) is True
    assert symptoms.delete_symptom(s["id"]) is False
    assert symptoms.get_symptom(s["id"]) is None
    assert [op for _, op, _, _ in _mutations()] == ["INSERT", "DELETE"]


# --- context bound ---

def test_context_bound_defaults_to_zero_and_persists():
    assert context.get_context_bound() == 0
    event_id = _write_event("user_message")
    context.set_context_bound(event_id)
    assert context.get_context_bound() == event_id
    context.set_context_bound(0)
    assert context.get_context_bound() == 0


def test_context_bound_rejects_unknown_event():
    with pytest.raises(ValueError, match="Unknown event id"):
        context.set_context_bound(999)


def test_turn_start_and_turn_root_id():
    assert context.turn_start(5) == 0
    assert context.turn_root_id(1) == 0
    first = _write_event("user_message")
    call = _write_event("api_call")
    second = _write_event("wakeup")
    _write_event("api_call")

    assert context.turn_start(call) == first
    assert context.turn_start(second) == second
    assert context.turn_root_id(1) == second
    assert context.turn_root_id(2) == first
    assert context.turn_root_id(3) == 0
