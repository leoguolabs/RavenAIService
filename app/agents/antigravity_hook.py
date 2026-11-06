"""Tiny stdin/stdout hook client; credentials exist only in the run environment."""
import json
import os
import sys
import urllib.request


def main():
    event = sys.argv[1]
    try:
        payload = {"event": event, "payload": json.load(sys.stdin)}
        request = urllib.request.Request(
            os.environ["RAVEN_CONTROL_URL"] + "/hook",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + os.environ["RAVEN_CONTROL_TOKEN"],
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3600) as response:
            sys.stdout.write(response.read().decode())
    except Exception:
        # Fail closed when Raven's permission broker is unavailable.
        if event == "PreToolUse":
            print(json.dumps({"decision": "deny", "reason": "Raven permission service unavailable"}))
        else:
            print("{}")


if __name__ == "__main__":
    main()
