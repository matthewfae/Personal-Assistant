"""
Wake-up jobs: the prompts Claude is woken with.

A job is started by a trigger (today a webhook; later the scheduler) and runs
as a silent wake-up turn: Claude only reaches the user by calling notify_user.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    name: str
    prompt: str


ARRIVED_HOME = Job(
    name="arrived_home",
    prompt=(
        "Matt just arrived home (his iPhone's arrival automation fired). "
        "Check open tasks and the shopping list. If something is worth his attention "
        "now that he's home (a home chore, groceries to put away, something due soon), "
        "tell him briefly with notify_user. If nothing is, don't message him."
    ),
)
