import requests
from bs4 import BeautifulSoup
import yaml
import re
import unicodedata

# Configuration
DOC_URL = "https://dev.twitch.tv/docs/eventsub/eventsub-reference/"
OUTPUT_FILE = "twitch_eventsub_swagger.yaml"

# Some fields are frequently described as arrays but the "Type" column is inconsistent.
# We'll use name-based heuristics as a safety net.
ARRAY_FIELD_NAME_HINTS = {
    "choices",
    "outcomes",
    "fragments",
    "emotes",
    "top_contributions",
    "top_predictors",
    "boundaries",
    "terms_found",
    "shared_ban_channel_ids",
    "types",
    "chat_rules_cited",
    "badges",
    "format",
    "participants",
    "data",
    "terms",
}


def to_pascal_case(text: str) -> str:
    """
    Strictly converts text to PascalCase using only standard ASCII letters and numbers.
    This prevents characters like Â (\xC2) from being included as 'A'.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-zA-Z0-9]+", text, re.ASCII)
    return "".join(word.capitalize() for word in words)


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _leading_indent(raw: str) -> int:
    """
    Measures indentation in the "Name" cell.
    Twitch tables often use leading spaces / NBSP to visually nest fields.
    """
    if not raw:
        return 0
    raw = raw.replace("\xa0", " ")
    return len(raw) - len(raw.lstrip(" "))


def _looks_nullable(type_text: str, desc_text: str) -> bool:
    t = (type_text or "").lower()
    d = (desc_text or "").lower()
    return ("null" in t) or ("(or null" in t) or (" null " in d) or (" is null" in d) or ("null if" in d)


def _force_array_if_needed(field_name: str, type_text: str, desc_text: str, schema: dict) -> dict:
    """
    If the docs strongly imply an array but we didn't parse it as an array, fix it.
    This addresses cases like choices/outcomes/fragments where the doc text says "An array..."
    """
    name = (field_name or "").strip()
    if not name:
        return schema

    desc_l = (desc_text or "").lower()
    type_l = (type_text or "").lower()

    implied_array = (
            name in ARRAY_FIELD_NAME_HINTS
            or desc_l.startswith("an array")
            or "an array of" in desc_l
            or type_l.startswith("array")
            or "array of" in type_l
            or "[]" in type_l
    )
    if implied_array and schema.get("type") != "array":
        # Best-effort: if schema is a $ref, make it the item type; else keep as object item.
        if "$ref" in schema:
            items = {"$ref": schema["$ref"]}
        else:
            items = schema if schema.get("type") else {"type": "object"}
        fixed = {"type": "array", "items": items}
        # Preserve description/nullable
        if "description" in schema:
            fixed["description"] = schema["description"]
        return fixed

    return schema


def map_type(twitch_type_raw: str, *, field_name: str | None = None, desc_text: str = "") -> dict:
    """Maps Twitch documentation types to OpenAPI types (best-effort)."""
    t_clean = _normalize_ws(twitch_type_raw).lower()
    # Remove parentheticals like (or null)
    t_base = re.sub(r"\(.*?\)", "", t_clean).strip()

    # Arrays (handle: "array", "array of X", "X[]")
    if "[]" in t_base or t_base.startswith("array") or "array of" in t_base:
        # Extract inner type name
        inner = (
            t_base.replace("array of", "")
            .replace("array", "")
            .replace("of", "")
            .replace("[]", "")
            .strip()
        )
        inner_mapping = map_type(inner, field_name=None, desc_text="") if inner and inner != "object" else {"type": "object"}
        res = {"type": "array", "items": inner_mapping}
        if _looks_nullable(twitch_type_raw, desc_text):
            res["nullable"] = True
        return res

    # Primitives
    res = None
    if any(x in t_base for x in ["timestamp", "date", "datetime", "rfc3339"]):
        res = {"type": "string"}  # could be format: date-time, but Twitch uses RFC3339 with varying precision
    elif any(x in t_base for x in ["string"]):
        res = {"type": "string"}
    elif any(x in t_base for x in ["bool", "boolean"]):
        res = {"type": "boolean"}
    elif any(x in t_base for x in ["int", "integer", "number", "float", "counter"]):
        # Twitch docs often say "integer"/"number" loosely; keep integer to match most payloads.
        res = {"type": "integer"}
    elif t_base in {"object", ""}:
        res = {"type": "object"}

    # ID is not always explicitly typed; if "id" appears in the type text, prefer string.
    if res is None and "id" in t_base:
        res = {"type": "string"}

    if res is not None:
        if _looks_nullable(twitch_type_raw, desc_text):
            res["nullable"] = True
        return res

    # Otherwise, treat it as a potential reference
    ref_name = to_pascal_case(t_base)
    if not ref_name or ref_name.lower() == "object":
        out = {"type": "object"}
    else:
        out = {"$ref": f"#/components/schemas/{ref_name}"}

    if _looks_nullable(twitch_type_raw, desc_text):
        # OpenAPI doesn't allow nullable next to $ref in a clean way without allOf; keep it simple:
        # wrap refs when needed.
        if "$ref" in out:
            out = {"allOf": [out], "nullable": True}
        else:
            out["nullable"] = True
    return out


def _ensure_object_schema(prop_schema: dict) -> dict:
    """
    Makes sure prop_schema can contain nested properties.
    If it's an array, nesting should happen under items (as object).
    """
    if prop_schema.get("type") == "array":
        items = prop_schema.setdefault("items", {"type": "object"})
        if items.get("type") != "object" and "properties" not in items:
            # If items is a $ref or primitive, we can't nest safely; fall back to object.
            prop_schema["items"] = {"type": "object"}
        prop_schema["items"].setdefault("type", "object")
        prop_schema["items"].setdefault("properties", {})
        return prop_schema["items"]

    prop_schema.setdefault("type", "object")
    prop_schema.setdefault("properties", {})
    return prop_schema


def parse_twitch_docs() -> dict:
    print(f"Fetching {DOC_URL}...")
    response = requests.get(
        DOC_URL,
        timeout=30,
        headers={"User-Agent": "eventsub-swagger-generator/1.1"},
    )
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")

    schemas: dict[str, dict] = {}

    headers = soup.find_all(["h2", "h3"])
    skip_titles = {"Contents", "Overview", "Request fields", "Response fields"}

    for header in headers:
        title = _normalize_ws(header.get_text())
        if title in skip_titles:
            continue

        table = header.find_next("table")
        if not table or table.find_previous(["h2", "h3"]) != header:
            continue

        component_name = to_pascal_case(title)
        if not component_name:
            continue

        rows = table.find_all("tr")
        if not rows:
            continue

        # Identify columns
        thead = [_normalize_ws(c.get_text()).lower() for c in rows[0].find_all(["th", "td"])]
        try:
            name_idx = next(i for i, v in enumerate(thead) if ("name" in v) or ("field" in v))
            type_idx = next(i for i, v in enumerate(thead) if "type" in v)
            desc_idx = next(i for i, v in enumerate(thead) if "description" in v)
        except StopIteration:
            continue

        required_idx = None
        for i, v in enumerate(thead):
            if "required" in v:
                required_idx = i
                break

        root_schema = {"type": "object", "properties": {}}
        required_fields: list[str] = []

        # Stack of (indent, current_object_schema, last_property_name_on_that_level)
        stack: list[tuple[int, dict, str | None]] = [(0, root_schema, None)]

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) <= max(name_idx, type_idx, desc_idx):
                continue

            raw_name = cells[name_idx].get_text()  # keep indentation
            indent = _leading_indent(raw_name)
            f_name = _normalize_ws(raw_name)

            # Some tables have empty separators; skip those
            if not f_name or f_name.lower() in {"-", "—"}:
                continue

            f_type = _normalize_ws(cells[type_idx].get_text())
            f_desc = _normalize_ws(cells[desc_idx].get_text())

            is_required = False
            if required_idx is not None and len(cells) > required_idx:
                req_val = _normalize_ws(cells[required_idx].get_text()).lower()
                is_required = req_val in {"yes", "true", "required"}

            # Adjust stack based on indentation
            while stack and indent < stack[-1][0]:
                stack.pop()
            if not stack:
                stack = [(0, root_schema, None)]

            # If indent increased, nest under the last property at previous level
            if indent > stack[-1][0]:
                prev_indent, prev_obj, prev_last_name = stack[-1]
                if prev_last_name and prev_last_name in prev_obj.get("properties", {}):
                    parent_prop_schema = prev_obj["properties"][prev_last_name]
                    nested_obj = _ensure_object_schema(parent_prop_schema)
                    stack.append((indent, nested_obj, None))
                # If we can't determine a parent, keep at current level (best effort).

            _, current_obj, _last = stack[-1]

            field_schema = map_type(f_type, field_name=f_name, desc_text=f_desc)
            field_schema["description"] = f_desc
            field_schema = _force_array_if_needed(f_name, f_type, f_desc, field_schema)

            # If nullable is implied by description, ensure it is set
            if _looks_nullable(f_type, f_desc):
                if "$ref" in field_schema:
                    field_schema = {"allOf": [{"$ref": field_schema["$ref"]}], "nullable": True, "description": f_desc}
                else:
                    field_schema.setdefault("nullable", True)

            current_obj.setdefault("properties", {})
            current_obj["properties"][f_name] = field_schema

            # record last property name for potential nesting
            stack[-1] = (stack[-1][0], current_obj, f_name)

            if is_required:
                required_fields.append(f_name)

        if root_schema["properties"]:
            if required_fields:
                # Deduplicate but keep order
                seen = set()
                root_schema["required"] = [x for x in required_fields if not (x in seen or seen.add(x))]
            schemas[component_name] = root_schema

    # Post-process refs: if a $ref points to a component we didn't find, fallback to object.
    valid_components = set(schemas.keys())

    def fix_schema(node: object) -> object:
        if isinstance(node, dict):
            # Fix $ref and allOf[$ref]
            if "$ref" in node:
                ref_target = node["$ref"].split("/")[-1]
                if ref_target not in valid_components:
                    return {"type": "object", "description": node.get("description", "")}
            if "allOf" in node and isinstance(node["allOf"], list):
                fixed_allof = []
                for part in node["allOf"]:
                    if isinstance(part, dict) and "$ref" in part:
                        ref_target = part["$ref"].split("/")[-1]
                        if ref_target not in valid_components:
                            fixed_allof.append({"type": "object"})
                        else:
                            fixed_allof.append(part)
                    else:
                        fixed_allof.append(part)
                node["allOf"] = fixed_allof

            for k, v in list(node.items()):
                node[k] = fix_schema(v)
        elif isinstance(node, list):
            return [fix_schema(x) for x in node]
        return node

    schemas = {k: fix_schema(v) for k, v in schemas.items()}

    # Add missing fields that the docs/examples guarantee exist in notification payloads:
    # Subscription.transport is present in webhook/websocket examples.
    if "Subscription" in schemas and "Transport" in schemas:
        sub_props = schemas["Subscription"].setdefault("properties", {})
        if "transport" not in sub_props:
            sub_props["transport"] = {"$ref": "#/components/schemas/Transport", "description": "Transport details."}

    return schemas


def main() -> None:
    schemas = parse_twitch_docs()

    spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "Twitch EventSub Reference",
            "version": "1.1.0",
            "description": "Best-effort OpenAPI components generated from Twitch EventSub Reference tables.",
        },
        "paths": {},
        "components": {"schemas": schemas},
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        yaml.dump(spec, f, sort_keys=False, default_flow_style=False, allow_unicode=True)

    print(f"Done! Created {OUTPUT_FILE} with {len(schemas)} schemas.")


if __name__ == "__main__":
    main()