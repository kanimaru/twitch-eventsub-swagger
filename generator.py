"""Generates an OpenAPI schema of the Twitch EventSub notifications from the EventSub reference.

The reference has one page per category (AutoMod, Channel, Chat, ...), each also served as Markdown. Every
subscription type there lists the fields of its subscription request body and of its notification payload as a
nested list whose entries carry their full path, type, flags and description:

    - `event.message.fragments` — Object[]. Ordered list of chat message fragments.
      - `event.message.fragments.type` — String, Nullable. The type of message fragment.

Each subscription type yields a <Name>Condition and a <Name>Event component, marked with x-subscription-type and
x-subscription-version. An object that several events document with the same shape (reward, outcomes, ...) becomes
one shared component named after its field.
"""
import copy
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import requests

REFERENCE_URL = "https://dev.twitch.tv/docs/eventsub/reference/"
INDEX_URL = "https://dev.twitch.tv/docs/eventsub/reference.md"
OUTPUT_FILE = "twitch_eventsub_swagger.json"

CATEGORY_LINK = re.compile(r"/docs/eventsub/reference/([a-z0-9-]+)\.md#")
ENTRY_HEADER = re.compile(r"^## (.+?) \{#([a-z0-9-]+)\}[ \t]*$", re.M)
SECTION_HEADER = re.compile(r"^### (.+?)[ \t]*$", re.M)
SUBSCRIPTION_LINE = re.compile(r"^- (Type|Version): `([^`]+)`", re.M)
FIELD_LINE = re.compile(
    r"^(?P<indent>\s*)- `(?P<path>[A-Za-z0-9_.]+)` — (?P<type>[A-Za-z]+)(?P<array>\[\])?"
    r"(?P<flags>(?:, (?:Nullable|\*\*Required\*\*))*)\.?(?:\s+(?P<description>.*))?$")
LIST_ITEM = re.compile(r"^\s*- (.*)$")
JSON_BLOCK = re.compile(r"^```json[ \t]*\n(.*?)^```", re.M | re.S)

PRIMITIVES = {"String": "string", "Integer": "integer", "Boolean": "boolean", "Number": "number"}

# Component base names that keep the names of the old single-page reference, so generated code keeps its classes.
# Keyed by the anchor of the subscription type; everything else is the PascalCase of its title.
BASE_NAME_OVERRIDES = {
    "channel-warning-acknowledge": "ChannelWarningAcknowledge",  # titled "Channel Warning Acknowledgement"
    "channel-custom-power-up-redemption-add": "ChannelCustomPowerUpRedemptionAdd",  # titled "Power-ups"
}
EVENT_NAME_OVERRIDES = {
    "automod-message-hold-v2": "AutomodMessageHoldEventV2",
    "automod-message-update-v2": "AutomodMessageUpdateEventV2",
    "channel-moderate-v2": "ChannelModerateEventV2",
}


@dataclass
class Field:
    path: str
    type: str
    is_array: bool
    nullable: bool
    required: bool
    description: str


@dataclass
class Entry:
    """One subscription type of the reference."""
    title: str
    anchor: str
    category: str
    subscription_type: str
    version: str
    request_fields: list = field(default_factory=list)
    payload_fields: list = field(default_factory=list)
    payload_examples: list = field(default_factory=list)  # JSON text of each event payload example

    @property
    def docs_url(self):
        return f"{REFERENCE_URL}{self.category}#{self.anchor}"

    @property
    def base_name(self):
        return BASE_NAME_OVERRIDES.get(self.anchor) or to_pascal_case(self.title)

    @property
    def event_name(self):
        return EVENT_NAME_OVERRIDES.get(self.anchor) or f"{self.base_name}Event"

    @property
    def condition_name(self):
        return f"{self.base_name}Condition"


def clean_text(text):
    """Turns Markdown into plain text: links keep their label, emphasis and code marks go."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*|`", "", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def to_pascal_case(text):
    """Strictly converts text to PascalCase using only ASCII alphanumeric chars."""
    text = unicodedata.normalize("NFKC", text).encode("ascii", "ignore").decode("ascii")
    return "".join(word.capitalize() for word in re.findall(r"[a-zA-Z0-9]+", text, re.ASCII))


def parse_fields(section):
    """Parses the field list of a Subscription Request Body or Event Payload section.

    A field's description is the text after its type plus the more deeply indented lines below it that are not
    fields themselves; a list there (the possible values) is joined with commas.
    """
    fields = []
    current, indent, parts, in_list = None, 0, [], False

    def finish():
        if current is not None:
            current.description = clean_text(" ".join(parts))

    for line in section.splitlines():
        match = FIELD_LINE.match(line)
        if match:
            finish()
            flags = match["flags"]
            current = Field(match["path"], match["type"], bool(match["array"]), "Nullable" in flags,
                            "Required" in flags, "")
            fields.append(current)
            indent, parts, in_list = len(match["indent"]), [match["description"] or ""], False
            continue
        if current is None or not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            continue  # text of the section, not of the field
        item = LIST_ITEM.match(line)
        if item:
            parts.append(("," if in_list else "") + " " + item.group(1))
            in_list = True
        else:
            parts.append(line.strip())
            in_list = False
    finish()
    for field_ in fields:
        field_.description = field_.description.replace(" ,", ",")
    return fields


def parse_page(category, markdown):
    """The subscription types of one category page."""
    entries = []
    headers = list(ENTRY_HEADER.finditer(markdown))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(markdown)
        body = markdown[header.end():end]
        subscription = dict(SUBSCRIPTION_LINE.findall(body))
        if "Type" not in subscription:
            continue  # "Contents" and other headers without a subscription type
        entry = Entry(header.group(1).strip(), header.group(2), category, subscription["Type"],
                      subscription.get("Version", "1"))
        sections = list(SECTION_HEADER.finditer(body))
        for position, section in enumerate(sections):
            text = body[section.end():sections[position + 1].start() if position + 1 < len(sections) else len(body)]
            title = section.group(1)
            if title == "Subscription Request Body":
                entry.request_fields = parse_fields(text)
            elif title == "Event Payload":
                entry.payload_fields = parse_fields(text)
            elif title.startswith("Event Payload Example"):
                entry.payload_examples.extend(block.group(1) for block in JSON_BLOCK.finditer(text))
        entries.append(entry)
    return entries


def fetch(url):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def fetch_reference():
    """Every subscription type of the EventSub reference, in the order of the reference index."""
    print(f"Fetching {INDEX_URL}...")
    categories = list(dict.fromkeys(CATEGORY_LINK.findall(fetch(INDEX_URL))))
    if not categories:
        raise RuntimeError(f"{INDEX_URL} links no category pages; has the reference moved again?")
    entries = []
    for category in categories:
        url = f"{REFERENCE_URL}{category}.md"
        print(f"Fetching {url}...")
        page_entries = parse_page(category, fetch(url))
        if not page_entries:
            raise RuntimeError(f"{url} has no subscription types; has its format changed?")
        entries.extend(page_entries)
    return entries


def field_schema(field_):
    schema = {"type": "object"} if field_.type == "Object" else {"type": PRIMITIVES.get(field_.type, "string")}
    if field_.type not in PRIMITIVES and field_.type != "Object":
        print(f"  WARNING unknown type {field_.type} of {field_.path}, using string")
    if field_.is_array:
        schema = {"type": "array", "items": schema}
    if field_.nullable:
        schema["nullable"] = True
    schema["description"] = field_.description
    return schema


def object_of(schema):
    """The object schema that holds the children of a field: the field itself, or the items of an array.

    A field documented as a primitive but with fields below it is an object.
    """
    target = schema["items"] if schema.get("type") == "array" else schema
    if target.get("type") != "object":
        kept = {key: target[key] for key in ("nullable", "description") if key in target}
        target.clear()
        target.update({"type": "object", **kept})
    return target


def build_object(fields, root_path):
    """Builds the schema of the field at root_path from the fields below it; None if it has none."""
    nodes = {root_path: {"type": "object"}}
    for field_ in fields:
        if field_.path == root_path or not field_.path.startswith(root_path + "."):
            continue
        parent_path, name = field_.path.rsplit(".", 1)
        if parent_path not in nodes:
            raise ValueError(f"{field_.path} is documented without its parent {parent_path}")
        parent = object_of(nodes[parent_path])
        schema = field_schema(field_)
        parent.setdefault("properties", {})[name] = schema
        if field_.required:
            parent.setdefault("required", []).append(name)
        nodes[field_.path] = schema
    root = nodes[root_path]
    root = root["items"] if root.get("type") == "array" else root
    return root if root.get("properties") else None


# "This field has the same information as the sub field but for ..." (shared_chat_* fields of chat
# notifications and moderate events) - documented without their own fields.
SAME_AS_FIELD = re.compile(r"same information as the (\w+) field")


def resolve_same_as(props):
    """Gives an object documented as "the same information as the X field" the schema of its sibling X."""
    for name, schema in list(props.items()):
        if schema.get("type") == "object" and schema.get("properties"):
            resolve_same_as(schema["properties"])
        if schema.get("type") != "object" or schema.get("properties"):
            continue
        match = SAME_AS_FIELD.search(schema.get("description", ""))
        if not match:
            continue
        # The docs sometimes name a field that does not exist (shared_chat_sub_gift says "chat_sub_gift"); the
        # field without the shared_chat_ prefix is the one meant.
        sibling = props.get(match.group(1)) or props.get(name.removeprefix("shared_chat_"))
        if sibling is None or sibling is schema:
            continue
        resolved = copy.deepcopy(sibling)
        resolved["description"] = schema["description"]
        if schema.get("nullable"):
            resolved["nullable"] = True
        props[name] = resolved


# Mistakes of the reference that the parser cannot see. Every correction is backed by the payloads Twitch sends -
# the examples of the reference and the Twitch CLI - and validate.py checks the generated schema against those
# payloads, so a wrong correction fails the build. Each entry is (component, field path, change, evidence); a path
# walks properties with "." and array items with "[]".
# Changes: {"rename": new name}, {"set": schema keys}, {"add": schema, "after": field} or {"same_as": sibling field}.
# "Old example" is an example of the single-page reference that the per-category pages replaced in November 2026.
SCHEMA_CORRECTIONS = [
    ("ChannelChatNotificationEvent", "shared_chat_unraid", {"same_as": "unraid"}, "old shared chat example"),
    ("ChannelChatNotificationEvent", "shared_chat_bits_badge_tier", {"same_as": "bits_badge_tier"},
     "old shared chat example"),
    ("ChannelChatNotificationEvent", "shared_chat_charity_donation", {"same_as": "charity_donation"},
     "old shared chat example"),
] + [
    (component, field, {"add": {"type": "string", "description": description}, "after": after}, "old example")
    for component in ("ChannelGuestStarSessionBeginEvent", "ChannelGuestStarSessionEndEvent")
    for field, description, after in (
        ("moderator_user_id", "The user ID of the moderator who started or ended the session.",
         "broadcaster_user_login"),
        ("moderator_user_name", "The display name of the moderator.", "moderator_user_id"),
        ("moderator_user_login", "The login of the moderator.", "moderator_user_name"),
    )
]


class CorrectionError(Exception):
    pass


def apply_corrections(schemas):
    """Applies SCHEMA_CORRECTIONS. A correction the docs no longer need is reported; one whose field is gone fails."""
    for component, path, change, evidence in SCHEMA_CORRECTIONS:
        if component not in schemas:
            raise CorrectionError(f"correction of {component}.{path}: the component no longer exists")
        *parents, name = path.split(".")
        props = schemas[component]["properties"]
        for parent in parents:
            schema = props[parent.removesuffix("[]")]
            if parent.endswith("[]"):
                schema = schema["items"]
            props = schema.setdefault("properties", {})
        field_name = name.removesuffix("[]")

        if "rename" in change:
            if field_name not in props and change["rename"] in props:
                print(f"  correction no longer needed: {component}.{path} is {change['rename']} ({evidence})")
                continue
            renamed = {change["rename"] if key == field_name else key: value for key, value in props.items()}
            props.clear()
            props.update(renamed)
        elif "set" in change:
            schema = props[field_name]
            if all(schema.get(key) == value for key, value in change["set"].items()):
                print(f"  correction no longer needed: {component}.{path} ({evidence})")
                continue
            if change["set"].get("type") not in (None, "object", "array"):
                schema.pop("properties", None)
                schema.pop("items", None)
            schema.update(change["set"])
        elif "add" in change:
            if field_name in props:
                print(f"  correction no longer needed: {component}.{path} exists ({evidence})")
                continue
            entries = list(props.items())
            index = next((i + 1 for i, (key, _) in enumerate(entries) if key == change["after"]), 0)
            entries.insert(index, (field_name, dict(change["add"])))
            props.clear()
            props.update(entries)
        elif "same_as" in change:
            if field_name in props:
                print(f"  correction no longer needed: {component}.{path} exists ({evidence})")
                continue
            sibling = copy.deepcopy(props[change["same_as"]])
            sibling["nullable"] = True
            sibling["description"] = f"This field has the same information as the {change['same_as']} field " \
                                     f"but for a notification that happened in a channel in the shared chat session."
            props[field_name] = sibling


def shape(schema):
    """The structure of a schema without its descriptions, for finding objects documented the same way twice."""
    return (schema.get("type"), schema.get("nullable", False),
            shape(schema["items"]) if "items" in schema else None,
            tuple((name, shape(child)) for name, child in schema.get("properties", {}).items()))


def liftable(schema):
    """The object a field could share as a component: the field's object, or the object items of its array."""
    target = schema.get("items", {}) if schema.get("type") == "array" else schema
    return target if target.get("type") == "object" and target.get("properties") else None


def share_repeated_objects(schemas):
    """Moves objects that several components document with the same shape into one component per field name.

    The name is the PascalCase of the field (reward -> Reward, top_predictors -> TopPredictors). When a field name
    comes with different shapes, the most frequent one is shared and the others stay inline.
    """
    shapes = defaultdict(Counter)

    def count(props):
        for name, schema in props.items():
            target = liftable(schema)
            if target is not None:
                shapes[to_pascal_case(name)][shape(target)] += 1
                count(target["properties"])

    for component in schemas.values():
        count(component.get("properties", {}))
    chosen = {}
    for name, counter in shapes.items():
        common, occurrences = counter.most_common(1)[0]
        if occurrences >= 2 and name not in schemas:
            chosen[name] = common

    shared = {}

    def replace(props):
        for name, schema in props.items():
            target = liftable(schema)
            if target is None:
                continue
            component = to_pascal_case(name)
            if chosen.get(component) != shape(target):
                replace(target["properties"])
                continue
            if component not in shared:
                lifted = copy.deepcopy(target)
                lifted.pop("description", None)
                lifted.pop("nullable", None)
                shared[component] = lifted
                replace(lifted["properties"])
            ref = {"$ref": f"#/components/schemas/{component}"}
            if schema.get("type") == "array":
                schema["items"] = ref
            else:
                nullable, description = schema.get("nullable"), schema.get("description", "")
                schema.clear()
                schema.update({"allOf": [ref], "nullable": True} if nullable else ref)
                schema["description"] = description

    for component in list(schemas.values()):
        replace(component.get("properties", {}))
    schemas.update(sorted(shared.items()))


def component(schema, entry):
    return {"type": "object", "x-docs-url": entry.docs_url, "x-subscription-type": entry.subscription_type,
            "x-subscription-version": entry.version, **schema}


def build_schemas(entries):
    schemas = {}
    for entry in entries:
        for name, fields, root in ((entry.condition_name, entry.request_fields, "condition"),
                                   (entry.event_name, entry.payload_fields, "event"),
                                   (entry.event_name, entry.payload_fields, "events")):
            schema = build_object(fields, root)
            if schema is None:
                continue
            if name in schemas:
                raise ValueError(f"{name} of {entry.docs_url} clashes with {schemas[name]['x-docs-url']}")
            schema.pop("type")
            schema.pop("description", None)
            schema.pop("nullable", None)
            resolve_same_as(schema["properties"])
            schemas[name] = component(schema, entry)
        if entry.condition_name not in schemas:
            print(f"  WARNING {entry.docs_url} documents no condition fields")
        if entry.event_name not in schemas:
            print(f"  WARNING {entry.docs_url} documents no event fields")

    # The envelope every notification and subscription shares, as the first subscription type documents it.
    first = entries[0]
    for name, fields, root in (("Subscription", first.payload_fields, "subscription"),
                               ("Transport", first.request_fields, "transport")):
        schema = build_object(fields, root)
        if root == "subscription":
            condition = schema["properties"]["condition"]
            condition.pop("properties", None)
            condition.pop("required", None)
        schema.pop("description", None)
        schemas[name] = {"type": "object", "x-docs-url": first.docs_url, **schema}
        schemas[name].pop("nullable", None)

    apply_corrections(schemas)
    share_repeated_objects(schemas)
    return schemas


def main():
    schemas = build_schemas(fetch_reference())

    openapi_spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "Twitch EventSub Reference",
            "version": "1.0.0",
            "description": "Auto-generated OpenAPI spec with clean UTF-8 encoding."
        },
        "paths": {},
        "components": {"schemas": schemas}
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        # ensure_ascii=False writes literal UTF-8 characters instead of \uXXXX
        json.dump(openapi_spec, f, indent=2, ensure_ascii=False)

    print(f"Successfully generated {OUTPUT_FILE} with {len(schemas)} schemas.")


if __name__ == "__main__":
    main()
