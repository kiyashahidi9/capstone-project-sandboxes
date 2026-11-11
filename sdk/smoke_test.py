"""Quick SDK-only check: create a sandbox, use every method, destroy it.

No OpenAI key needed, just a running API (the mock, or the real one via SANDBOX_API).

Run from the repo root: python -m sdk.smoke_test
"""

## IMPORTS ##

from sdk.sandbox import Sandbox


## SMOKE TEST ##

def main() -> None:
    """
    Runs every SDK method once against the API
    prints what was run and what came back
    always destroys the sandbox
    """
    sandbox = Sandbox.create()
    print(f"create() -> {sandbox}")

    try:
        # foreground command: waits and returns its output
        result = sandbox.exec("echo hi")
        print(f"\nexec('echo hi') -> {result}")

        # write a file, then read it back
        sandbox.write_file("/root/sdk-test/x.txt", "hello")
        print("\nwrite_file('/root/sdk-test/x.txt', 'hello')")

        content = sandbox.read_file("/root/sdk-test/x.txt")
        print(f"\nread_file('/root/sdk-test/x.txt') -> {content!r}")

        # background command: returns a pid right away
        result = sandbox.exec("sleep 5", background=True)
        print(f"\nexec('sleep 5', background=True) -> {result}")

    finally:
        # always clean up, even if something crashed
        sandbox.destroy()
        print(f"\ndestroy() -> {sandbox.id} destroyed")


if __name__ == "__main__":
    main()
