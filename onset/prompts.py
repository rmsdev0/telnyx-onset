"""System prompt composition, agent configuration, and the Golden Fork demo.

The composer is lifted from voice-agent-lite: build_system_prompt rebuilds the
prompt on every LLM call so it reflects the live conversation state (collected
slots, current flow node, time of day). The Golden Fork config (Ava, the
restaurant reservation assistant) recreates the voice-agent-lite restaurant
demo verbatim: identity, greeting, three flow nodes, rules, and the three
tools from onset.tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from onset.tools import restaurant_tools

if TYPE_CHECKING:
    from onset.conversation import CallContext
    from onset.tools import ToolRegistry


@dataclass
class FlowNode:
    """A node in a conversation flow graph.

    Each node defines what the agent should do in that state, which tools
    are available, and how to transition to the next state.
    """

    task: str
    tools: list[str] | None = None  # None means all tools available
    transitions: dict[str, str] = field(default_factory=dict)  # tool_name -> next_node
    terminal: bool = False  # end the call once this node's response finishes


@dataclass
class AgentConfig:
    """Configuration for a voice agent's behavior."""

    name: str = "Voice Agent"
    persona: str = "a helpful phone assistant"
    instructions: str = ""
    # Spoken first when the call connects, before the caller says anything.
    # Empty means the agent waits for the caller to speak first.
    greeting: str = ""
    response_style: str = (
        "Keep every reply to one or two short sentences and get to the point. "
        "Ask one question at a time and do not over-explain. "
        "Never use bullet points, numbered lists, or labeled fields like "
        "'Name:' or 'Date:'; the caller only hears your voice, so speak every "
        "detail inline as part of a sentence."
    )
    guardrails: list[str] = field(default_factory=list)
    tools: ToolRegistry | None = None
    nodes: dict[str, FlowNode] | None = None
    initial_node: str | None = None
    # Spoken to the caller when the response pipeline fails or the tool loop
    # exceeds max_tool_rounds, instead of going silent
    fallback_message: str = (
        "I'm sorry, I wasn't able to complete that. Could you try again?"
    )
    # Cap on LLM tool-call rounds per user turn before falling back
    max_tool_rounds: int = 5
    # Spoken once when the per-call token budget runs out
    budget_exceeded_message: str = (
        "I'm sorry, but I have to wrap up this call now. "
        "Please call back if you need anything else. Goodbye!"
    )


def build_system_prompt(
    config: AgentConfig,
    context: CallContext | None = None,
    current_node: str | None = None,
) -> str:
    """Compose the system prompt from config, context, and flow state.

    Rebuilt on each LLM call so it reflects the current conversation state
    (collected slots, current flow node, time of day, and so on).
    """
    sections: list[str] = []

    # Identity
    sections.append(f"You are {config.name}, {config.persona}.")

    # Voice-call framing: the agent must know it is on a live phone call, or it
    # tends to claim it is reading or typing text.
    sections.append(
        "You are on a live phone call. The caller speaks to you and hears your "
        "replies as speech. You can only hear the caller and speak back; you cannot "
        "see, read, or type anything. Never say you are reading or typing. "
        "If a turn seems garbled, cut off, or like background noise or another "
        "person talking rather than the caller, do not act on it; briefly ask the "
        "caller to repeat."
    )

    # Response style
    sections.append(config.response_style)

    # Flow node task (if using flow control)
    node: FlowNode | None = None
    node_key = current_node or (context.current_node if context else None)
    if config.nodes and node_key and node_key in config.nodes:
        node = config.nodes[node_key]
        sections.append(f"Current task: {node.task}")
    elif config.instructions:
        sections.append(config.instructions)

    # Dynamic context
    if context:
        context_parts: list[str] = []
        # Local server time so the agent can resolve relative dates. Without the
        # date, the LLM guesses (it invented 2024-12-20 for "tomorrow").
        now = datetime.now().astimezone()
        context_parts.append(
            f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p %Z')}. "
            "Resolve relative dates like 'today', 'tomorrow', and 'this Friday' "
            "from this."
        )
        if context.from_number:
            context_parts.append(f"Caller number: {context.from_number}")
        if context.slots:
            slot_str = ", ".join(f"{k}: {v}" for k, v in context.slots.items())
            context_parts.append(f"Information collected so far: {slot_str}")
        if context_parts:
            sections.append("Context:\n" + "\n".join(f"- {p}" for p in context_parts))

    # Guardrails
    if config.guardrails:
        rules = "\n".join(f"- {g}" for g in config.guardrails)
        sections.append(f"Rules:\n{rules}")

    return "\n\n".join(sections)


# ── Golden Fork restaurant demo config ───────────────────────────

# The spoken greeting handles the opening line, so the flow starts at "booking".
# Each node exposes the tool needed to progress, and calling that tool advances
# the flow: booking -> (check_availability) -> confirm -> (make_reservation) ->
# farewell. The farewell node is terminal, so the call hangs up after the
# goodbye finishes playing.
_NODES: dict[str, FlowNode] = {
    "booking": FlowNode(
        task=(
            "Help the caller book a table. Collect the date, time, and party "
            "size, asking for any missing detail one question at a time. As "
            "soon as you have all three, call check_availability. You can also "
            "answer menu questions with get_menu."
        ),
        tools=["check_availability", "get_menu"],
        transitions={"check_availability": "confirm"},
    ),
    "confirm": FlowNode(
        task=(
            "Availability has been checked. If a table is available, ask for "
            "the caller's name and then call make_reservation to finalize. If "
            "it is not available, suggest another date or time and call "
            "check_availability again."
        ),
        tools=["make_reservation", "check_availability"],
        transitions={
            "make_reservation": "farewell",
            "check_availability": "confirm",
        },
    ),
    "farewell": FlowNode(
        task=(
            "The reservation is confirmed. In one or two short spoken "
            "sentences, naturally weave the key details (name, date, time, "
            "party size) and the confirmation number into flowing speech, then "
            "thank the caller and say goodbye. Never use a list, bullet points, "
            "or labeled fields like 'Name:' or 'Date:'. The caller only hears "
            "your voice, so say it the way you would aloud."
        ),
        tools=[],
        terminal=True,
    ),
}

# The captured prompt.txt instructions from the parity target (DISCOVERY 3.1).
# build_system_prompt injects instructions only when no flow node is active, and
# a node is always active here, so this matches the reference config verbatim
# without changing runtime behavior; the same facts are enforced by the tools
# and node tasks.
_INSTRUCTIONS = (
    "You are Ava, the phone assistant for The Golden Fork restaurant.\n\n"
    "You help callers check availability, make reservations, and answer "
    "questions about the menu. You are warm, professional, and efficient.\n\n"
    "The Golden Fork is open Tuesday through Sunday, 5 PM to 10 PM. We are "
    "closed on Mondays. We seat parties of up to 8 people. For larger groups, "
    "ask the caller to email events@goldenfork.example.com.\n\n"
    "Always confirm the details before making a reservation: date, time, party "
    "size, and name."
)

RESTAURANT_CONFIG = AgentConfig(
    name="Ava",
    persona="the friendly phone assistant for The Golden Fork restaurant",
    greeting="Thank you for calling The Golden Fork. How can I help you today?",
    instructions=_INSTRUCTIONS,
    guardrails=[
        "Do not make up availability or reservation details.",
        "Do not accept reservations for more than 8 people.",
        "If unsure, offer to transfer to a staff member.",
    ],
    tools=restaurant_tools,
    nodes=_NODES,
    initial_node="booking",
)
