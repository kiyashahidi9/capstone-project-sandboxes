TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the sandbox (working directory /root). "
                           "Returns exit_code, stdout and stderr. With background=true it returns "
                           "a pid immediately and output goes to /tmp/bg-<pid>.log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "The shell command to run."},
                    "background": {"type": "boolean", "description": "Start it and return immediately."},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute path."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file in the sandbox, creating parent folders. Overwrites it if it exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path."},
                    "content": {"type": "string", "description": "The full new contents."},
                },
                "required": ["path", "content"],
            },
        },
    },
]
