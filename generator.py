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

def map_type(twitch_type_raw, description_text=""):
    """Maps Twitch types to OpenAPI with robust nullable detection."""
    t_clean = twitch_type_raw.lower().strip()
    d_clean = description_text.lower()

    # Determine if nullable
    is_nullable = any(word in t_clean for word in ['null', 'optional']) or \
                  any(word in d_clean for word in ['field is null', 'can be null', 'is optional'])

    # Remove parentheticals for type mapping
    t_base = re.sub(r'\(.*\)', '', t_clean).strip()

    # 1. Handle Arrays
    if 'array' in t_base or '[]' in t_base:
        inner = t_base.replace('array', '').replace('of', '').replace('[]', '').strip()
        inner_mapping = map_type(inner) if inner and inner != 'object' else {"type": "object"}
        res = {"type": "array", "items": inner_mapping}
        if is_nullable: res["nullable"] = True
        return res

    # 2. Handle Primitives
    res = None
    if any(x in t_base for x in ['string', 'timestamp', 'date', 'id']):
        res = {"type": "string"}
    elif any(x in t_base for x in ['int', 'integer', 'number', 'float', 'counter']):
        res = {"type": "integer"}
    elif any(x in t_base for x in ['bool', 'boolean']):
        res = {"type": "boolean"}

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
        return {"anyOf": [ref_dict, {"nullable": True}]}

    return ref_dict

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

        rows = table.find_all('tr')
        if not rows: continue

        thead = [c.get_text().lower() for c in rows[0].find_all(['th', 'td'])]
        try:
            name_idx = next(i for i, v in enumerate(thead) if 'name' in v or 'field' in v)
            type_idx = next(i for i, v in enumerate(thead) if 'type' in v)
            desc_idx = next(i for i, v in enumerate(thead) if 'description' in v)
            req_idx = next((i for i, v in enumerate(thead) if 'required' in v), -1)
        except (StopIteration, ValueError):
            continue

        for row in rows[1:]:
            cells = row.find_all('td')
            if len(cells) <= max(name_idx, type_idx, desc_idx): continue

            # Clean property name
            f_name = cells[name_idx].get_text(strip=True)
            f_type = cells[type_idx].get_text(strip=True)
            f_desc = clean_description(cells[desc_idx])

            field_schema = map_type(f_type, f_desc)
            field_schema["description"] = f_desc
            properties[f_name] = field_schema

            if req_idx != -1 and 'yes' in cells[req_idx].get_text().lower():
                required_fields.append(f_name)

        if properties:
            schemas[component_name] = {
                "type": "object",
                "x-docs-url": f"{DOC_URL}#{header_id}" if header_id else DOC_URL,
                "properties": properties
            }
            if required_fields:
                schemas[component_name]["required"] = required_fields

    # Post-Process Validation
    valid_components = set(schemas.keys())
    for comp in schemas.values():
        for prop_name, prop_val in comp.get("properties", {}).items():
            target_ref = None
            if "$ref" in prop_val: target_ref = prop_val
            elif "anyOf" in prop_val: target_ref = prop_val["anyOf"][0]
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