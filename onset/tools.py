"""Tool registration, dispatch, and the restaurant demo tools.

The registry and @tool decorator are lifted from voice-agent-lite: they
auto-generate OpenAI-format JSON schemas from Python type hints and execute
tools by name against a JSON argument string, injecting the CallContext when
the function accepts it.

The three Golden Fork tools recreate the voice-agent-lite restaurant demo
verbatim, with one deliberate change: the reservation confirmation number uses
zlib.crc32 instead of the builtin hash(), so it is stable across runs and the
demo is reproducible.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_type_hints

import structlog

# Imported at runtime (not under TYPE_CHECKING) because the @tool decorator
# calls get_type_hints, which evaluates the CallContext annotation on the demo
# tools. A type-checking-only import would raise NameError there.
from onset.conversation import CallContext  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()

# Maps Python types to JSON Schema types
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


@dataclass
class ToolDef:
    """A registered tool with its function and generated schema."""

    name: str
    description: str
    func: Callable[..., Any]
    parameters: dict[str, Any]


class ToolRegistry:
    """Registry of tools available to the voice agent."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def tool(
        self, description: str = ""
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a function as an agent tool.

        The function's name, type hints, and docstring generate the
        OpenAI-format function schema automatically.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            name = func.__name__
            desc = description or func.__doc__ or name

            # Build JSON Schema from type hints
            hints = get_type_hints(func)
            hints.pop("return", None)
            sig = inspect.signature(func)

            properties: dict[str, Any] = {}
            required: list[str] = []

            for param_name, param in sig.parameters.items():
                if param_name in ("self", "cls", "context", "call_context"):
                    continue
                python_type = hints.get(param_name, str)
                json_type = _TYPE_MAP.get(python_type, "string")
                properties[param_name] = {"type": json_type}
                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

            parameters = {
                "type": "object",
                "properties": properties,
                "required": required,
            }

            self._tools[name] = ToolDef(
                name=name,
                description=desc,
                func=func,
                parameters=parameters,
            )
            return func

        return decorator

    def get_schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI-format tool schemas for passing to the LLM."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "strict": True,
                },
            }
            for t in self._tools.values()
        ]

    def subset_schemas(self, names: list[str]) -> list[dict[str, Any]]:
        """Return schemas for only the named tools."""
        return [
            schema
            for schema in self.get_schemas()
            if schema["function"]["name"] in names
        ]

    async def execute(self, name: str, arguments_json: str, **extra_kwargs: Any) -> str:
        """Execute a tool by name and return its string result.

        Arguments are passed as a JSON string (as received from the LLM).
        Extra kwargs (for example call_context) are passed if the function
        accepts them. Errors are returned as strings rather than raised.
        """
        tool_def = self._tools.get(name)
        if not tool_def:
            log.warning("tools.unknown", name=name)
            return f"Error: unknown tool '{name}'"

        try:
            kwargs = json.loads(arguments_json)
        except json.JSONDecodeError as e:
            log.warning("tools.invalid_json", name=name, error=str(e))
            return f"Error: invalid arguments JSON: {e}"

        # Pass call_context if the function accepts it
        sig = inspect.signature(tool_def.func)
        for extra_name, extra_val in extra_kwargs.items():
            if extra_name in sig.parameters:
                kwargs[extra_name] = extra_val

        try:
            log.info("tools.execute", name=name, arguments=kwargs)
            result = tool_def.func(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            return str(result)
        except Exception as e:
            log.exception("tools.error", name=name)
            return f"Error executing {name}: {e}"


# ── Golden Fork restaurant demo tools ────────────────────────────

restaurant_tools = ToolRegistry()


@restaurant_tools.tool(
    description="Check if a table is available for the given date, time, and party size"
)
async def check_availability(
    date: str, time: str, party_size: int, call_context: CallContext | None = None
) -> str:
    # In production this would query a real reservation system.
    # For the demo, tables are always available except Mondays.
    if "monday" in date.lower():
        return "Sorry, the restaurant is closed on Mondays."

    if party_size > 8:
        return (
            "We cannot seat parties larger than 8. Please email "
            "events@goldenfork.example.com for large group bookings."
        )

    # Store collected info in call context
    if call_context:
        call_context.slots["date"] = date
        call_context.slots["time"] = time
        call_context.slots["party_size"] = party_size

    return f"A table for {party_size} is available on {date} at {time}."


@restaurant_tools.tool(
    description="Confirm and make a reservation with the caller's name"
)
async def make_reservation(name: str, call_context: CallContext | None = None) -> str:
    if call_context:
        call_context.slots["name"] = name
        date = call_context.slots.get("date", "the requested date")
        time = call_context.slots.get("time", "the requested time")
        party_size = call_context.slots.get("party_size", "your party")
        # crc32 is deterministic across runs, unlike the builtin hash(), so the
        # confirmation number is stable and the demo is reproducible.
        number = zlib.crc32(name.encode()) % 10000
        return (
            f"Reservation confirmed for {name}: "
            f"party of {party_size} on {date} at {time}. "
            f"Confirmation number: GF-{number:04d}."
        )
    return f"Reservation confirmed for {name}."


@restaurant_tools.tool(description="Get the current menu highlights")
async def get_menu() -> str:
    return (
        "Tonight's specials: "
        "Pan-seared salmon with lemon butter, $32. "
        "Truffle mushroom risotto, $28. "
        "Grilled ribeye with roasted vegetables, $45. "
        "All entrees come with a house salad."
    )
