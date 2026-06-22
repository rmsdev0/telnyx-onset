"""Tests for the restaurant demo tools and the schema generator."""

from __future__ import annotations

import zlib

import pytest

from onset.conversation import CallContext
from onset.tools import restaurant_tools


@pytest.mark.asyncio
async def test_check_availability_closed_on_monday() -> None:
    result = await restaurant_tools.execute(
        "check_availability",
        '{"date": "Monday", "time": "7 PM", "party_size": 2}',
        call_context=CallContext(),
    )
    assert result == "Sorry, the restaurant is closed on Mondays."


@pytest.mark.asyncio
async def test_check_availability_party_too_large() -> None:
    result = await restaurant_tools.execute(
        "check_availability",
        '{"date": "Friday", "time": "7 PM", "party_size": 9}',
        call_context=CallContext(),
    )
    assert "cannot seat parties larger than 8" in result


@pytest.mark.asyncio
async def test_check_availability_stores_slots() -> None:
    ctx = CallContext()
    result = await restaurant_tools.execute(
        "check_availability",
        '{"date": "Friday", "time": "7 PM", "party_size": 4}',
        call_context=ctx,
    )
    assert result == "A table for 4 is available on Friday at 7 PM."
    assert ctx.slots == {"date": "Friday", "time": "7 PM", "party_size": 4}


@pytest.mark.asyncio
async def test_make_reservation_is_deterministic() -> None:
    ctx = CallContext(slots={"date": "Friday", "time": "7 PM", "party_size": 4})
    first = await restaurant_tools.execute(
        "make_reservation", '{"name": "Alex"}', call_context=ctx
    )
    second = await restaurant_tools.execute(
        "make_reservation", '{"name": "Alex"}', call_context=ctx
    )
    expected_number = zlib.crc32(b"Alex") % 10000
    assert f"GF-{expected_number:04d}" in first
    assert first == second  # stable across runs, unlike builtin hash()
    assert "party of 4 on Friday at 7 PM" in first


@pytest.mark.asyncio
async def test_get_menu() -> None:
    result = await restaurant_tools.execute("get_menu", "{}")
    assert "Pan-seared salmon" in result
    assert "Truffle mushroom risotto" in result


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_string() -> None:
    result = await restaurant_tools.execute("not_a_tool", "{}")
    assert result.startswith("Error: unknown tool")


@pytest.mark.asyncio
async def test_invalid_json_returns_error_string() -> None:
    result = await restaurant_tools.execute("get_menu", "{not json")
    assert result.startswith("Error: invalid arguments JSON")


def test_schema_shape() -> None:
    schemas = {s["function"]["name"]: s for s in restaurant_tools.get_schemas()}
    assert set(schemas) == {"check_availability", "make_reservation", "get_menu"}

    check = schemas["check_availability"]["function"]
    assert check["strict"] is True
    props = check["parameters"]["properties"]
    # call_context is injected, not exposed to the model.
    assert "call_context" not in props
    assert props["party_size"]["type"] == "integer"
    assert props["date"]["type"] == "string"
    assert set(check["parameters"]["required"]) == {"date", "time", "party_size"}


def test_subset_schemas_gates_by_name() -> None:
    subset = restaurant_tools.subset_schemas(["get_menu"])
    assert [s["function"]["name"] for s in subset] == ["get_menu"]
    # The terminal farewell node offers no tools.
    assert restaurant_tools.subset_schemas([]) == []
