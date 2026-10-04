import copy
import requests
from bs4 import BeautifulSoup
import json
import re
import unicodedata

# Configuration
DOC_URL = "https://dev.twitch.tv/docs/eventsub/eventsub-reference/"
OUTPUT_FILE = "twitch_eventsub_swagger.json"

def clean_description(cell):
    """
    Extracts text from a table cell, ensuring spaces between HTML elements 
    to prevent squashing, and normalizes unicode characters.
    """
    # separator=' ' prevents "text<li>item</li>" from becoming "textitem"
    raw_text = cell.get_text(separator=' ', strip=True)

    # Normalize unicode (converts non-breaking spaces to standard spaces, etc.)
    text = unicodedata.normalize("NFKC", raw_text)

    # Optional: Convert curly quotes to straight quotes for better compatibility
    text = text.replace('’', "'").replace('“', '"').replace('”', '"')

    # Clean up multiple spaces
    return re.sub(r'\s+', ' ', text).strip()

def to_pascal_case(text):
    """Strictly converts text to PascalCase using only ASCII alphanumeric chars."""
    # Strip any non-ascii garbage before regex
    text = unicodedata.normalize("NFKC", text).encode('ascii', 'ignore').decode('ascii')
    words = re.findall(r'[a-zA-Z0-9]+', text, re.ASCII)
    return "".join(word.capitalize() for word in words)

# "An array of outcomes...", "The ordered list of fragments...",
# "An array that includes the emote ID..." - Twitch documents most collections
# in prose only, without any marker in the Type column.
ARRAY_IN_DESCRIPTION = re.compile(r'\b(?:an?|the)\s+(?:\w+\s+)?(?:array|list)\b|\b(?:array|list)\s+of\b')

def map_primitive(t_base):
    """Maps a bare Twitch type name to a primitive schema, or None if it isn't one.

    Matches whole words only: a reference type such as "channel_points_voting" contains "int" and "id"-like
    fragments without being an integer or a string.
    """
    words = set(re.findall(r'[a-z0-9]+', t_base))
    if words & {'string', 'timestamp', 'date', 'datetime', 'id'}:
        return {"type": "string"}
    if words & {'int', 'integer', 'int32', 'int64', 'number', 'float', 'counter'}:
        return {"type": "integer"}
    if words & {'bool', 'boolean'}:
        return {"type": "boolean"}
    return None

def map_type(twitch_type_raw, description_text="", infer_collection=True):
    """Maps Twitch types to OpenAPI with robust nullable detection.

    infer_collection is cleared when mapping the item type of an array, so a
    plural type name such as "outcomes" yields Outcomes[] and not Outcomes[][].
    """
    t_clean = twitch_type_raw.lower().strip()
    d_clean = description_text.lower()

    # Determine if nullable
    is_nullable = any(word in t_clean for word in ['null', 'optional']) or \
                  any(word in d_clean for word in ['field is null', 'can be null', 'is optional'])

    # Remove parentheticals for type mapping
    t_base = re.sub(r'\(.*\)', '', t_clean).strip()

    # 1. Handle Arrays
    is_array = 'array' in t_base or '[]' in t_base
    inner = t_base
    if is_array:
        inner = re.sub(r'\barray\b|\bof\b|\[\s*\]', '', t_base).strip()
    elif infer_collection and map_primitive(t_base) is None:
        # Reference types (e.g. "outcomes", "choices", "top_contributions") carry
        # no [] in the Type column, so the collection has to be inferred. Both
        # signals are only trusted for non-primitives: a boolean whose prose
        # mentions a list is still a boolean.
        is_array = bool(ARRAY_IN_DESCRIPTION.search(d_clean)) or \
                   (t_base.endswith('s') and t_base != 'object')

    if is_array:
        if inner == 'object': inner = '' # If it was 'object (list of ...)', inner will be 'object'
        inner_mapping = map_type(inner, infer_collection=False) if inner and inner != 'object' else {"type": "object"}
        res = {"type": "array", "items": inner_mapping}
        if is_nullable: res["nullable"] = True
        return res

    # 2. Handle Primitives
    res = map_primitive(t_base)

    if res:
        if is_nullable: res["nullable"] = True
        return res

    # 3. Handle References
    ref_name = to_pascal_case(t_base)
    if not ref_name or ref_name.lower() == "object":
        obj = {"type": "object"}
        if is_nullable: obj["nullable"] = True
        return obj

    ref_dict = {"$ref": f"#/components/schemas/{ref_name}"}
    if is_nullable:
        # The OpenAPI 3.0 way to make a reference nullable ($ref ignores sibling keys).
        return {"allOf": [ref_dict], "nullable": True}

    return ref_dict

# "This field has the same information as the sub field but for ..." (shared_chat_* fields of chat
# notifications and moderate events) - documented without their own rows.
SAME_AS_FIELD = re.compile(r'same information as the (\w+) field')


def resolve_same_as(props):
    """Gives an object documented as "the same information as the X field" the schema of its sibling X."""
    for name, schema in list(props.items()):
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


def reference_flattened_objects(props, schemas):
    """Links an object without children to the component of the same name.

    Some tables list an object's fields without indentation, right after it (transport of the conduit shard
    disabled event). When a childless object is named like a component, it references that component, and the
    rows directly after it that are fields of that component are folded back into it.
    """
    names = list(props.keys())
    for index, name in enumerate(names):
        schema = props.get(name)
        if schema is None or schema.get("type") != "object" or schema.get("properties"):
            continue
        component = schemas.get(to_pascal_case(name))
        if component is None or component is props:
            continue
        ref = {"$ref": f"#/components/schemas/{to_pascal_case(name)}"}
        props[name] = ({"allOf": [ref], "nullable": True} if schema.get("nullable") else ref) | \
            {"description": schema.get("description", "")}
        component_fields = component.get("properties", {})
        for follower in names[index + 1:]:
            if follower not in component_fields:
                break
            del props[follower]


# Mistakes of the reference tables that the parser cannot see. Every correction is backed by the payloads Twitch
# sends - the examples of the subscription types page and the Twitch CLI - and validate.py checks the generated
# schema against those payloads, so a wrong correction fails the build. Each entry is
# (component, field path, change, evidence); a path walks properties with "." and array items with "[]".
# Changes: {"rename": new name}, {"set": schema keys}, {"add": schema, "after": field} or {"same_as": sibling field}.
SCHEMA_CORRECTIONS = [
    ("ChannelUnbanRequestResolveEvent", "moderator_id", {"rename": "moderator_user_id"},
     "docs example and Twitch CLI"),
    ("ChannelUnbanRequestResolveEvent", "moderator_login", {"rename": "moderator_user_login"},
     "docs example and Twitch CLI"),
    ("ChannelUnbanRequestResolveEvent", "moderator_name", {"rename": "moderator_user_name"},
     "docs example and Twitch CLI"),
    ("ChannelChatNotificationEvent", "chatter_user_login",
     {"add": {"type": "string", "description": "The chatter's login name."}, "after": "chatter_user_name"},
     "both docs examples"),
    ("ChannelChatNotificationEvent", "message.text", {"set": {"type": "string"}}, "both docs examples"),
    ("ChannelChatNotificationEvent", "shared_chat_unraid", {"same_as": "unraid"}, "shared chat docs example"),
    ("ChannelChatNotificationEvent", "shared_chat_bits_badge_tier", {"same_as": "bits_badge_tier"},
     "shared chat docs example"),
    ("ChannelChatNotificationEvent", "shared_chat_charity_donation", {"same_as": "charity_donation"},
     "shared chat docs example"),
    ("ChannelChatUserMessageHoldEvent", "message.fragments[].type",
     {"add": {"type": "string", "description": "The type of message fragment. Possible values: text, emote, "
                                               "cheermote."}, "after": None},
     "docs example"),
    ("ChannelChatUserMessageUpdateEvent", "message.fragments[].type",
     {"add": {"type": "string", "description": "The type of message fragment. Possible values: text, emote, "
                                               "cheermote."}, "after": None},
     "docs example"),
    ("ChannelSuspiciousUserMessageEvent", "message.fragments[].cheermote.bits", {"set": {"type": "integer"}},
     "docs example; the cheermote of every other message fragment"),
    ("ChannelSuspiciousUserMessageEvent", "message.fragments[].cheermote.tier", {"set": {"type": "integer"}},
     "docs example; the cheermote of every other message fragment"),
] + [
    (component, field, {"add": {"type": "string", "description": description}, "after": after}, "docs example")
    for component in ("ChannelGuestStarSessionBeginEvent", "ChannelGuestStarSessionEndEvent")
    for field, description, after in (
        ("moderator_user_id", "The user ID of the moderator who started or ended the session.",
         "broadcaster_user_login"),
        ("moderator_user_name", "The display name of the moderator.", "moderator_user_id"),
        ("moderator_user_login", "The login of the moderator.", "moderator_user_name"),
    )
]


def apply_corrections(schemas):
    """Applies SCHEMA_CORRECTIONS. A correction the docs no longer need is reported; one whose field is gone fails."""
    for component, path, change, evidence in SCHEMA_CORRECTIONS:
        *parents, name = path.split(".")
        props = schemas[component]["properties"]
        for parent in parents:
            schema = props[parent.removesuffix("[]")]
            if parent.endswith("[]"):
                schema = schema["items"]
            props = schema.setdefault("properties", {})
        field = name.removesuffix("[]")

        if "rename" in change:
            if field not in props and change["rename"] in props:
                print(f"  correction no longer needed: {component}.{path} is {change['rename']} ({evidence})")
                continue
            renamed = {change["rename"] if key == field else key: value for key, value in props.items()}
            props.clear()
            props.update(renamed)
        elif "set" in change:
            schema = props[field]
            if all(schema.get(key) == value for key, value in change["set"].items()):
                print(f"  correction no longer needed: {component}.{path} ({evidence})")
                continue
            if change["set"].get("type") not in (None, "object", "array"):
                schema.pop("properties", None)
                schema.pop("items", None)
            schema.update(change["set"])
        elif "add" in change:
            if field in props:
                print(f"  correction no longer needed: {component}.{path} exists ({evidence})")
                continue
            entries = list(props.items())
            index = next((i + 1 for i, (key, _) in enumerate(entries) if key == change["after"]), 0)
            entries.insert(index, (field, dict(change["add"])))
            props.clear()
            props.update(entries)
        elif "same_as" in change:
            if field in props:
                print(f"  correction no longer needed: {component}.{path} exists ({evidence})")
                continue
            sibling = copy.deepcopy(props[change["same_as"]])
            sibling["nullable"] = True
            sibling["description"] = f"This field has the same information as the {change['same_as']} field " \
                                     f"but for a notification that happened in a channel in the shared chat session."
            props[field] = sibling


def parse_twitch_docs():
    print(f"Fetching {DOC_URL}...")
    response = requests.get(DOC_URL)
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, 'html.parser')

    schemas = {}
    headers = soup.find_all(['h2', 'h3'])

    for header in headers:
        title = header.get_text().strip()
        header_id = header.get('id', '')

        # Skip Navigational noise
        if title in ["Contents", "Overview", "Request fields", "Response fields"]:
            continue

        table = header.find_next("table")
        if not table or table.find_previous(['h2', 'h3']) != header:
            continue

        component_name = to_pascal_case(title)
        properties = {}
        required_fields = []
        stack = [(0, properties)] # Reset stack for each component
        last_field = None # (depth, schema) of the previous row

        rows = table.find_all('tr')
        if not rows: continue

        thead = [c.get_text().lower() for c in rows[0].find_all(['th', 'td'])]
        try:
            name_idx = next(i for i, v in enumerate(thead) if 'name' in v or 'field' in v or 'param' in v)
            type_idx = next(i for i, v in enumerate(thead) if 'type' in v)
            desc_idx = next(i for i, v in enumerate(thead) if 'description' in v)
            req_idx = next((i for i, v in enumerate(thead) if 'required' in v), -1)
        except (StopIteration, ValueError):
            continue

        for row in rows[1:]:
            cells = row.find_all('td')
            if len(cells) <= max(name_idx, type_idx, desc_idx): continue

            # Clean property name and determine indentation
            name_cell = cells[name_idx]
            
            # Count leading non-breaking spaces explicitly in the cell contents
            indent_count = 0
            # Twitch documentation sometimes uses multiple strings or elements
            # We want the indentation before the FIRST element (usually a <code>)
            for child in name_cell.children:
                child_text = str(child)
                stripped = child_text.lstrip('\xa0 ')
                if not stripped: # Only spaces/nbsps
                    indent_count += len(child_text)
                else:
                    # Found something with content, add its leading spaces and stop
                    indent_count += len(child_text) - len(stripped)
                    break
            
            # Each level of indentation in Twitch docs is usually 3 non-breaking spaces
            depth = indent_count // 3
            if '\xa0 \xa0' in str(name_cell): depth = 1 # Workaround
            
            f_name = name_cell.get_text(strip=True)
            # Remove any zero-width characters or other junk if necessary
            f_name = "".join(c for c in f_name if c.isprintable()).strip()

            f_type = cells[type_idx].get_text(strip=True)
            f_desc = clean_description(cells[desc_idx])
            
            # Rows indented under a field documented as a primitive make it an object (charity_donation of
            # channel.chat.notification is typed "string" but has charity_name and amount below it).
            if last_field is not None and depth > last_field[0] and \
                    last_field[1].get("type") in ("string", "integer", "boolean"):
                last_depth, last_schema = last_field
                nullable = last_schema.get("nullable")
                description = last_schema["description"]
                last_schema.clear()
                last_schema.update({"type": "object", "properties": {}, "description": description})
                if nullable: last_schema["nullable"] = True
                stack.append((last_depth + 1, last_schema["properties"]))

            # Use a stack to handle nested properties
            while stack and stack[-1][0] > depth and len(stack) > 1:
                stack.pop()

            # Use the actual properties dict from the stack
            current_props = stack[-1][1]

            # Map the type
            field_schema = map_type(f_type, f_desc)
            field_schema["description"] = f_desc
            
            # Add to current level
            current_props[f_name] = field_schema
            last_field = (depth, field_schema)

            # If it's a container, push to stack
            is_object = field_schema.get("type") == "object"
            is_object_array = field_schema.get("type") == "array" and field_schema.get("items", {}).get("type") == "object"
            
            if is_object or is_object_array:
                if is_object_array:
                    field_schema["items"]["properties"] = {}
                    new_props = field_schema["items"]["properties"]
                else:
                    field_schema["properties"] = {}
                    new_props = field_schema["properties"]
                
                stack.append((depth + 1, new_props))

            if req_idx != -1 and 'yes' in cells[req_idx].get_text().lower():
                required_fields.append(f_name)
        
        # Cleanup: Remove empty properties objects if no children were found
        def remove_empty_properties(props):
            for p_name, p_val in list(props.items()):
                # Check for properties in the object itself or in its items if it's an array
                if p_val.get("type") == "object":
                    if "properties" in p_val:
                        if not p_val["properties"]:
                            del p_val["properties"]
                        else:
                            remove_empty_properties(p_val["properties"])
                elif p_val.get("type") == "array" and "items" in p_val:
                    if p_val["items"].get("type") == "object":
                        if "properties" in p_val["items"]:
                            if not p_val["items"]["properties"]:
                                del p_val["items"]["properties"]
                            else:
                                remove_empty_properties(p_val["items"]["properties"])

        remove_empty_properties(properties)
        resolve_same_as(properties)

        if properties:
            schemas[component_name] = {
                "type": "object",
                "x-docs-url": f"{DOC_URL}#{header_id}" if header_id else DOC_URL,
                "properties": properties
            }
            if required_fields:
                schemas[component_name]["required"] = required_fields

    apply_corrections(schemas)

    # Post-Process Validation
    valid_components = set(schemas.keys())
    for comp in schemas.values():
        reference_flattened_objects(comp.get("properties", {}), schemas)
        for prop_name, prop_val in comp.get("properties", {}).items():
            target_ref = None
            if "$ref" in prop_val: target_ref = prop_val
            elif "allOf" in prop_val: target_ref = prop_val["allOf"][0]
            elif prop_val.get("type") == "array" and "$ref" in prop_val.get("items", {}):
                target_ref = prop_val["items"]

            if target_ref and "$ref" in target_ref:
                ref_name = target_ref["$ref"].split("/")[-1]
                if ref_name not in valid_components:
                    desc = prop_val.get("description", "")
                    comp["properties"][prop_name] = {"type": "object", "description": desc}

    return schemas

def main():
    schemas = parse_twitch_docs()

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

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        # ensure_ascii=False writes literal UTF-8 characters instead of \uXXXX
        json.dump(openapi_spec, f, indent=2, ensure_ascii=False)

    print(f"Successfully generated {OUTPUT_FILE} with {len(schemas)} schemas.")

if __name__ == "__main__":
    main()