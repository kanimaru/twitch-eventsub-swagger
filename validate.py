"""Checks the generated schema against real EventSub notifications.

The schema is parsed from the tables of the EventSub reference, which are easy to misread (indentation, prose-only
arrays, unindented fields). The payloads Twitch sends are the ground truth, so every notification payload is
validated against the schema of its event and condition:

- the example notifications of the subscription types page (EXAMPLES_URL), which link each payload to the
  reference section of its event and condition,
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

import requests
from bs4 import BeautifulSoup

from generator import OUTPUT_FILE

EXAMPLES_URL = "https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/"
FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
REFERENCE_PATH = "/docs/eventsub/eventsub-reference"  # linked with and without the trailing slash

# Mistakes in a payload source, not in the schema: (source, subscription type, version, path) -> reason, where
# source is "docs", "twitch-cli" or "fixture" and array indices are written []. Only list a mismatch here when
# another Twitch source contradicts the payload; an entry that no longer occurs fails the run.
KNOWN_PAYLOAD_ERRORS = {
    ("docs", "channel.ad_break.begin", "1", "event.duration_seconds"): "a string; the Twitch CLI sends an integer",
    ("docs", "channel.ad_break.begin", "1", "event.is_automatic"): "a string; the Twitch CLI sends a boolean",
    ("docs", "channel.channel_points_custom_reward.remove", "1", "condition.reward_id"):
        "an integer; the Twitch CLI sends a string",
    **{("docs", f"channel.prediction.{kind}", "1", "event.outcomes[].top_predictors[].user_id"):
        "an integer; the Twitch CLI sends a string" for kind in ("progress", "lock", "end")},
    **{("twitch-cli", f"channel.charity_campaign.{kind}", "1", f"event.broadcaster_user_{field}"):
        "the reference table and the docs example say broadcaster_" + field
        for kind in ("start", "progress", "stop") for field in ("id", "login", "name")},
    **{("docs", f"automod.message.{kind}", "1", path): reason
        for kind in ("hold", "update")
        for path, reason in (("event.message", "a string; the reference table documents the object of V2"),
                             ("event.fragments", "outside the message; the reference table has it inside"))},
    ("docs", "automod.settings.update", "1", "event.data"):
        "wraps the event in the data array of the Get AutoMod Settings response",
    **{("docs", "channel.chat.notification", "1", f"event.{field}.sub_plan"):
        "sub_plan; the reference table documents sub_tier and is_prime" for field in ("resub", "shared_chat_resub")},
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


def component_for_anchor(schemas):
    """Maps the reference anchor of each component (x-docs-url) to its name."""
    return {schema["x-docs-url"].split("#", 1)[1]: name
            for name, schema in schemas.items() if "#" in schema.get("x-docs-url", "")}


def reference_anchor(table, field):
    """The reference anchor the type cell of a field links to, or None."""
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2 and cells[0].get_text(strip=True) == field:
            link = cells[1].find("a", href=True)
            if link and REFERENCE_PATH in link["href"] and "#" in link["href"]:
                return link["href"].split("#", 1)[1]
    return None


SUBSCRIPTION_HEADER = re.compile(r"^[a-z_]+(\.[a-z_]+)+\b")


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


def example_payloads():
    """(source, payload, event anchor, condition anchor) of every example notification of the docs.

    The anchors come from the section the example is in: the event type cell of its Notification Payload table
    and the condition type cell of its Request Body table.
    """
    print(f"Fetching {EXAMPLES_URL}...")
    response = requests.get(EXAMPLES_URL)
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")

    # Walk the page in order: a subscription type header (automod.message.hold V2) starts a section, its tables give
    # the anchors, and the examples after them use those.
    event_anchor = condition_anchor = None
    header = None
    for element in soup.find_all(["h2", "h3", "pre"]):
        if element.name != "pre":
            header = element
            title = element.get_text().strip()
            if SUBSCRIPTION_HEADER.match(title):
                event_anchor = condition_anchor = None
            elif title.endswith("Payload"):  # "... Notification Payload", once "Channel Chat Message Payload"
                event_anchor = reference_anchor(element.find_next("table"), "event")
            elif title.endswith("Request Body"):
                condition_anchor = reference_anchor(element.find_next("table"), "condition")
            continue

        text = element.get_text()
        if '"event"' not in text or '"subscription"' not in text:
            continue
        source = f"{EXAMPLES_URL}#{header.get('id') if header else ''}"
        try:
            payload = lenient_json(text)
        except json.JSONDecodeError as error:
            print(f"  WARNING {source}: example is not JSON ({error})")
            continue
        if isinstance(payload, dict) and "event" in payload:
            yield source, payload, event_anchor, condition_anchor


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
    anchors = component_for_anchor(schemas)

    failures = []
    warnings = []
    unlinked = set()
    known_seen = set()
    checked = 0
    event_for_type, condition_for_type, versions = {}, {}, {}

    def validate(source, payload, event_anchor, condition_anchor):
        nonlocal checked
        key = subscription_key(payload)
        kind = source.split(" ", 1)[0] if source.startswith("twitch-cli") else \
            "fixture" if source.startswith("fixtures/") else "docs"
        for part, anchor in (("event", event_anchor), ("condition", condition_anchor)):
            value = payload.get("event") if part == "event" else payload.get("subscription", {}).get("condition")
            if value is None:
                continue
            if anchor is None:
                # The docs link no reference section (conditions of charity, shield mode, shoutout, guest star).
                unlinked.add(f"{key[0]} {part}")
                continue
            component = anchors.get(anchor)
            if component is None:
                warnings.append(f"{source}: no schema for the {part} of {key[0]} v{key[1]} (anchor {anchor})")
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

    for source, payload, event_anchor, condition_anchor in example_payloads():
        key = subscription_key(payload)
        event_for_type.setdefault(key, event_anchor)
        condition_for_type.setdefault(key, condition_anchor)
        versions.setdefault(key[0], set()).add(key[1])
        validate(source, payload, event_anchor, condition_anchor)
    for source, payload in [*cli_payloads(versions), *fixture_payloads()]:
        key = subscription_key(payload)
        validate(source, payload, event_for_type.get(key), condition_for_type.get(key))

    print(f"Validated {checked} payload parts against {OUTPUT_FILE}.")
    if unlinked:
        print(f"  Not checked, the docs link no schema: {', '.join(sorted(unlinked))}")
    for line in warnings:
        print(f"  WARNING {line}")
    for known in sorted(set(KNOWN_PAYLOAD_ERRORS) - known_seen):
        failures.append(f"KNOWN_PAYLOAD_ERRORS entry no longer occurs, remove it: {known}")
    if failures:
        print(f"{len(failures)} mismatches between the schema and real payloads:")
        for line in failures:
            print(f"  {line}")
        sys.exit(1)


if __name__ == "__main__":
    main()
