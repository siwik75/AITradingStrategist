"""
Tool Schema Builder — Generates Anthropic tool_use schemas from Python functions.
Compatible with both Anthropic native API and OpenAI-standard (GHO Gateway).
"""

import inspect
from typing import Any, get_type_hints

TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    Any: "string",
    str | None: "string",
    int | None: "integer",
    float | None: "number",
}


def build_tool_schema(fn) -> dict:
    """Convert a Python function into an Anthropic tool_use schema."""
    hints = get_type_hints(fn)
    doc = inspect.getdoc(fn) or ""

    # Parse docstring for param descriptions
    param_docs = {}
    for line in doc.split("\n"):
        if ":param" in line:
            parts = line.split(":param")[1].split(":")
            if len(parts) >= 2:
                param_docs[parts[0].strip()] = parts[1].strip()

    properties = {}
    required = []

    sig = inspect.signature(fn)
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue

        py_type = hints.get(name, Any)
        # Handle Optional types
        origin = getattr(py_type, "__origin__", None)
        if origin is not None:
            args = getattr(py_type, "__args__", ())
            if type(None) in args:
                py_type = next(a for a in args if a is not type(None))

        json_type = TYPE_MAP.get(py_type, "string")

        prop = {"type": json_type, "description": param_docs.get(name, f"Parameter {name}")}
        properties[name] = prop

        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "name": fn.__name__,
        "description": doc.split("\n")[0] if doc else fn.__name__,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


def build_tool_schema_openai(fn) -> dict:
    """Convert to OpenAI function calling format (for GHO Gateway)."""
    anthropic_schema = build_tool_schema(fn)
    return {
        "type": "function",
        "function": {
            "name": anthropic_schema["name"],
            "description": anthropic_schema["description"],
            "parameters": anthropic_schema["input_schema"],
        },
    }


def register_tools(*fns) -> list[dict]:
    """Register multiple functions as Anthropic tools."""
    return [build_tool_schema(fn) for fn in fns]


def register_tools_openai(*fns) -> list[dict]:
    """Register multiple functions as OpenAI-format tools (GHO)."""
    return [build_tool_schema_openai(fn) for fn in fns]
