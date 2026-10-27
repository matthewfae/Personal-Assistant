"""
Wake-up jobs: the prompts Claude is woken with.

A job is started by a trigger (today a webhook; later the scheduler) and runs
as a silent wake-up turn: Claude only reaches the user by calling notify_user.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Job:
    name: str
    prompt: str
    # Restricted MCP tools this job may use (see agent._RESTRICTED_TOOLS).
    extra_tools: frozenset[str] = field(default_factory=frozenset)


ARRIVED_HOME = Job(
    name="arrived_home",
    prompt=(
        "Matt just arrived home (his iPhone's arrival automation fired). "
        "Check open tasks and the shopping list. If something is worth his attention "
        "now that he's home (a home chore, groceries to put away, something due soon), "
        "tell him briefly with notify_user. If nothing is, don't message him."
    ),
)

WEEKEND_PLAN = Job(
    name="weekend_plan",
    prompt=(
        "It's Saturday morning. Look at open tasks and the shopping list, and suggest "
        "a few things Matt could realistically get done this weekend: favor things that "
        "are due soon, high priority, or small enough to knock out. Send it with "
        "notify_user as a short list, a sentence or two each. If there's genuinely "
        "nothing worth suggesting, don't message him."
    ),
)

TRIM_CONTEXT = Job(
    name="trim_context",
    prompt=(
        "Weekly housekeeping. Read through the conversation history. First, if it "
        "mentions anything that should be in tasks or the shopping list but isn't "
        "(a task finished, a new to-do, an item bought), fix that. Then call "
        "set_context_bound to trim the history: keep the turns that are still "
        "unresolved or likely to come up again, drop the rest. Don't message Matt."
    ),
    extra_tools=frozenset({"set_context_bound"}),
)
