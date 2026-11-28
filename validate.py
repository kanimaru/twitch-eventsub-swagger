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
KNOWN_PAYLOAD_ERRORS = {}


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


def source_kind(source):
    """The payload source a source label belongs to, as KNOWN_PAYLOAD_ERRORS names it."""
    if source.startswith("twitch-cli"):
        return "twitch-cli"
    if source.startswith("fixtures/"):
        return "fixture"
    return "docs"


def payload_parts(payload):
    """(part, value) of the condition and every event of a notification; a missing part has the value None.

    A batching subscription type (drop.entitlement.grant) sends its events as a list, named events[0], events[1], ...
    """
    events = payload["events"] if isinstance(payload.get("events"), list) else [payload.get("event")]
    single = len(events) == 1 and "event" in payload
    return [("condition", payload.get("subscription", {}).get("condition"))] + [
        ("event" if single else f"events[{index}]", event) for index, event in enumerate(events)
    ]


def known_error_key(kind, key, path):
    """The KNOWN_PAYLOAD_ERRORS key of a mismatch: array indices of the path are written []."""
    return kind, key[0], key[1], re.sub(r"\[\d+\]", "[]", path)


class PayloadReport:
    """Validates notification payloads against the schemas and collects what the run fails on.

    A mismatch listed in known_errors is not a failure, but a known_errors entry that no mismatch matched is: the
    payload source was fixed, so the entry has to go.
    """

    def __init__(self, schemas, known_errors):
        self.schemas = schemas
        self.components = components_for_subscription(schemas)
        self.known_errors = known_errors
        self.mismatches = []
        self.unlinked = set()
        self.known_seen = set()
        self.checked = 0

    def validate(self, source, payload, key=None):
        key = key or subscription_key(payload)
        kind = source_kind(source)
        for part, value in payload_parts(payload):
            if value is None:
                continue
            name = part.split("[", 1)[0]
            component = self.components.get(key, {}).get(name.rstrip("s"))
            if component is None:
                self.unlinked.add(f"{key[0]} v{key[1]} {name}")
                continue
            validator = Validator(self.schemas)
            validator.check(value, {"$ref": f"#/components/schemas/{component}"}, part)
            self.checked += 1
            for path, message in validator.errors:
                known = known_error_key(kind, key, path)
                if known in self.known_errors:
                    self.known_seen.add(known)
                    continue
                self.mismatches.append(f"{source} ({component}): {key[0]} v{key[1]} {path} {message}")

    def stale_known_errors(self):
        return sorted(set(self.known_errors) - self.known_seen)

    def failures(self):
        return [
            *self.mismatches,
            *(f"no schema for the payload of {line}" for line in sorted(self.unlinked)),
            *(f"KNOWN_PAYLOAD_ERRORS entry no longer occurs, remove it: {known}"
              for known in self.stale_known_errors()),
        ]


def main():
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        schemas = json.load(f)["components"]["schemas"]
    entries = fetch_reference()
    report = PayloadReport(schemas, KNOWN_PAYLOAD_ERRORS)

    versions = {}
    for entry in entries:
        versions.setdefault(entry.subscription_type, set()).add(entry.version)
    for source, payload, key in example_payloads(entries):
        report.validate(source, payload, key)
    for source, payload in [*cli_payloads(versions), *fixture_payloads()]:
        report.validate(source, payload)

    print(f"Validated {report.checked} payload parts against {OUTPUT_FILE}.")
    failures = report.failures()
    if failures:
        print(f"{len(failures)} mismatches between the schema and real payloads:")
        for line in failures:
            print(f"  {line}")
        sys.exit(1)


if __name__ == "__main__":
    main()
