"""Tool schemas the agent can call (OpenAI function-calling format)."""


def build_tools(workdir: str) -> list[dict]:
    """Builds the tool schemas, telling the model which directory it works in."""
    path_rule = f"Relative paths are resolved against {workdir}; absolute paths are used as-is."

    return [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": (
                    f"Run a shell command in the sandbox. Commands run in {workdir}. "
                    "Set background=true for servers."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string"},
                        "background": {"type": "boolean"},
                    },
                    "required": ["cmd"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": f"Read a file in the sandbox. {path_rule}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": f"Write a file in the sandbox. {path_rule}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
    ]
