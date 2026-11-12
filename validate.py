"""Checks the generated schema against real EventSub notifications.

The schema is parsed from the field lists of the EventSub reference, which can be wrong or incomplete. The payloads
Twitch sends are the ground truth, so every notification payload is validated against the schema of its event and
condition, found by the x-subscription-type and x-subscription-version of the components:

- the event payload examples of the reference, next to the field lists the schema comes from,
- the mock notifications of the Twitch CLI, and
- the payloads in fixtures/ - notifications captured from Twitch, one JSON file each in the same format
  ({"subscription": {"type", "version", "condition"}, "event"}).

A payload field the schema does not know, an array documented as an object (or the other way round) and a wrong
primitive type fail the run, so a broken schema - and a wrong SCHEMA_CORRECTIONS entry of the generator - is never
committed. Fields the schema has but a payload leaves out are fine (optional fields, other event variants), and so
is null.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

from generator import OUTPUT_FILE, fetch_reference

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

# Mistakes in a payload source, not in the schema: (source, subscription type, version, path) -> reason, where
# source is "docs", "twitch-cli" or "fixture" and array indices are written []. Only list a mismatch here when
# another Twitch source contradicts the payload; an entry that no longer occurs fails the run.
KNOWN_PAYLOAD_ERRORS = {
    ("docs", "channel.ad_break.begin", "1", "event.duration_seconds"): "a string; the Twitch CLI sends an integer",
    ("docs", "channel.ad_break.begin", "1", "event.is_automatic"): "a string; the Twitch CLI sends a boolean",
    **{("docs", f"channel.prediction.{kind}", "1", "event.outcomes[].top_predictors[].user_id"):
        "an integer; the Twitch CLI sends a string" for kind in ("progress", "lock", "end")},
    **{("twitch-cli", f"channel.charity_campaign.{kind}", "1", f"event.broadcaster_user_{field}"):
        "the reference and the docs example say broadcaster_" + field
        for kind in ("start", "progress", "stop") for field in ("id", "login", "name")},
    ("docs", "channel.chat.notification", "1", "event.resub.sub_plan"):
        "sub_plan; the reference documents sub_tier and is_prime",
}


class Validator:
    def __init__(self, schemas):
        self.schemas = schemas
        self.errors = []

    def resolve(self, schema):
        """Follows $ref and the allOf wrapper of nullable references; keeps the outer nullable flag."""
        nullable = schema.get("nullable", False)
        while True:
            if "$ref" in schema:
                schema = self.schemas[schema["$ref"].split("/")[-1]]
            elif "allOf" in schema:
                schema = schema["allOf"][0]
            else:
                return schema, nullable or schema.get("nullable", False)

    def check(self, value, schema, path):
        schema, nullable = self.resolve(schema)
        if value is None:
            # Twitch sends null for many fields the docs do not mark as nullable; null never decides the shape.
            return
        kind = schema.get("type")
        if kind == "object":
            if not isinstance(value, dict):
                return self.fail(path, f"is {type_name(value)}, schema says object")
            properties = schema.get("properties")
            if properties is None:
                return  # untyped object
            for key, item in value.items():
                if key not in properties:
                    self.fail(f"{path}.{key}", "is not in the schema")
                else:
                    self.check(item, properties[key], f"{path}.{key}")
        elif kind == "array":
            if not isinstance(value, list):
                return self.fail(path, f"is {type_name(value)}, schema says array")
            for index, item in enumerate(value):
                self.check(item, schema.get("items", {}), f"{path}[{index}]")
        elif kind in ("string", "integer", "boolean"):
            if type_name(value) != kind:
                self.fail(path, f"is {type_name(value)}, schema says {kind}")

    def fail(self, path, message):
        self.errors.append((path, message))


def type_name(value):
    if isinstance(value, bool): return "boolean"
    if isinstance(value, int): return "integer"
    if isinstance(value, float): return "number"
    if isinstance(value, str): return "string"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    return "null"


def components_for_subscription(schemas):
    """Maps (subscription type, version) to the names of its "event" and "condition" components."""
    found = {}
    for name, schema in schemas.items():
        key = (schema.get("x-subscription-type"), schema.get("x-subscription-version"))
        if key[0] is not None:
            found.setdefault(key, {})["condition" if name.endswith("Condition") else "event"] = name
    return found


def lenient_json(text):
    """Parses an example of the docs, which may carry // comments, trailing commas or miss closing brackets."""
    stripped, in_string, index = [], False, 0
    while index < len(text):
        char = text[index]
        if in_string:
            stripped.append(char)
            if char == "\\":
                stripped.append(text[index + 1:index + 2])
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            stripped.append(char)
        elif text.startswith("//", index):
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        else:
            stripped.append(char)
        index += 1
    text = re.sub(r",(\s*[}\]])", r"\1", "".join(stripped))
    for closers in ("", "}", "}}", "}}}"):
        try:
            return json.loads(text + closers)
        except json.JSONDecodeError as error:
            last_error = error
    raise last_error


def example_payloads(entries):
    """(source, payload, key) of every event payload example of the reference.

    The key is the subscription type and version the example is documented under: an example's own subscription
    is sometimes copied from a neighbour (the automod.message.update v2 example says automod.message.hold).
    """
    for entry in entries:
        for text in entry.payload_examples:
            try:
                payload = lenient_json(text)
            except json.JSONDecodeError as error:
                print(f"  WARNING {entry.docs_url}: example is not JSON ({error})")
                continue
            if not isinstance(payload, dict) or ("event" not in payload and "events" not in payload):
                continue
            key = (entry.subscription_type, entry.version)
            if subscription_key(payload) != key:
                print(f"  WARNING {entry.docs_url}: example says {subscription_key(payload)}, documented as {key}")
            yield entry.docs_url, payload, key


def cli_payloads(versions):
    """("twitch-cli", payload) of every event the Twitch CLI mocks, in each version the docs have examples for.

    The CLI is Twitch's own tool, so its payloads are a second source next to the docs. Without `twitch` on the PATH
    the step is skipped, unless --require-cli is given (the workflow does).
    """
    if shutil.which("twitch") is None:
        if "--require-cli" in sys.argv:
            sys.exit("twitch CLI not found (--require-cli)")
        print("  WARNING twitch CLI not found, its payloads are not checked")
        return
    help_text = subprocess.run(["twitch", "event", "trigger", "--help"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL).stdout
    supported = re.search(r"\[([a-z_. ]+)\]", help_text)
    for subscription_type in (supported.group(1).split() if supported else []):
        for version in sorted(versions.get(subscription_type, ())):
            result = subprocess.run(["twitch", "event", "trigger", subscription_type, "-D", "-v", version],
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                print(f"  WARNING twitch CLI has no {subscription_type} v{version}: {result.stdout.strip()[:120]}")
                continue
            if subscription_key(payload) != (subscription_type, version):
                print(f"  WARNING twitch CLI sent {subscription_key(payload)} for {subscription_type} v{version}")
                continue
            yield f"twitch-cli {subscription_type} v{version}", payload


def fixture_payloads():
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        yield f"fixtures/{path.name}", json.loads(path.read_text(encoding="utf-8"))


def subscription_key(payload):
    subscription = payload.get("subscription", {})
    return subscription.get("type"), subscription.get("version")


def main():
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        schemas = json.load(f)["components"]["schemas"]
    components = components_for_subscription(schemas)
    entries = fetch_reference()

    failures = []
    unlinked = set()
    known_seen = set()
    checked = 0

    def validate(source, payload, key=None):
        nonlocal checked
        key = key or subscription_key(payload)
        kind = "twitch-cli" if source.startswith("twitch-cli") else             "fixture" if source.startswith("fixtures/") else "docs"
        # A batching subscription type (drop.entitlement.grant) sends its events as a list.
        events = payload["events"] if isinstance(payload.get("events"), list) else [payload.get("event")]
        parts = [("condition", payload.get("subscription", {}).get("condition"))] +                 [("event" if len(events) == 1 and "event" in payload else f"events[{index}]", event)
                 for index, event in enumerate(events)]
        for part, value in parts:
            if value is None:
                continue
            component = components.get(key, {}).get(part.split("[", 1)[0].rstrip("s"))
            if component is None:
                unlinked.add(f"{key[0]} v{key[1]} {part.split('[', 1)[0]}")
                continue
            validator = Validator(schemas)
            validator.check(value, {"$ref": f"#/components/schemas/{component}"}, part)
            checked += 1
            for path, message in validator.errors:
                known = (kind, key[0], key[1], re.sub(r"\[\d+\]", "[]", path))
                if known in KNOWN_PAYLOAD_ERRORS:
                    known_seen.add(known)
                    continue
                failures.append(f"{source} ({component}): {key[0]} v{key[1]} {path} {message}")

    versions = {}
    for entry in entries:
        versions.setdefault(entry.subscription_type, set()).add(entry.version)
    for source, payload, key in example_payloads(entries):
        validate(source, payload, key)
    for source, payload in [*cli_payloads(versions), *fixture_payloads()]:
        validate(source, payload)

    print(f"Validated {checked} payload parts against {OUTPUT_FILE}.")
    for line in sorted(unlinked):
        failures.append(f"no schema for the payload of {line}")
    for known in sorted(set(KNOWN_PAYLOAD_ERRORS) - known_seen):
        failures.append(f"KNOWN_PAYLOAD_ERRORS entry no longer occurs, remove it: {known}")
    if failures:
        print(f"{len(failures)} mismatches between the schema and real payloads:")
        for line in failures:
            print(f"  {line}")
        sys.exit(1)


if __name__ == "__main__":
    main()
