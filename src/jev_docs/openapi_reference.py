"""Claims from the OpenAPI document embedded in the canonical API reference page.

Only explicit schema declarations count. Examples, descriptions and summaries never
establish fields, defaults or requiredness; the single prose-dependent claim
(answer correlation) is gated by a reviewed schema hash. Anything ambiguous yields
no claim rather than a partial contract.
"""

import hashlib
import json
import re
from urllib.parse import urlsplit

# docs.typesafe.ai/api/system-one.md (page SHA-256
# a5c68bb00afd2c75504b5a9a07a497253f2b03380953b56934ddd80767f63c3d when reviewed):
# SystemOneResponse.properties.answers, canonical JSON. Its description states
# "Answers keyed by the question names supplied in the request."
REVIEWED_ANSWERS_SCHEMA_SHA256 = "df25b58c4e5e3736026f0b28156fc7f10414b98824b9feb9d83d3bb954ba51df"
ANSWERS_INTERPRETATION = "answers map keyed by question id"

_FENCE = re.compile(
    r"^(?P<fence>`{3,}|~{3,})yaml[ \t]+\S+[ \t]+(?P<method>[a-z]+)[ \t]+(?P<path>/\S*)[ \t]*\n"
    r"(?P<body>.*?)^(?P=fence)[ \t]*$",
    re.M | re.S,
)
_REF = re.compile(r"#/components/schemas/([A-Za-z0-9_.-]+)")
_QUESTION_TYPE = re.compile(r"[a-z][a-z0-9_-]*")


def schema_sha256(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load(body):
    """Return (data, node) for one plain YAML document, or None when ambiguous."""
    import yaml  # Extraction only; privileged publication validation never parses YAML.

    try:
        for event in yaml.parse(body, Loader=yaml.SafeLoader):
            # Anchors/aliases can alias or amplify structure; the reference needs neither.
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                return None
        node = yaml.compose(body, Loader=yaml.SafeLoader)
        if node is None:
            return None
        pending = [node]
        while pending:
            current = pending.pop()
            if isinstance(current, yaml.MappingNode):
                keys = [key.value for key, _ in current.value if isinstance(key, yaml.ScalarNode)]
                # PyYAML keeps the last duplicate silently; a repeated key is ambiguous.
                if len(keys) != len(current.value) or len(set(keys)) != len(keys):
                    return None
                pending.extend(value for _, value in current.value)
            elif isinstance(current, yaml.SequenceNode):
                pending.extend(current.value)
        data = yaml.SafeLoader("").construct_document(node)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError):
        return None
    return (data, node) if isinstance(data, dict) else None


class _Located:
    """Pair constructed values with the source lines that declare them."""

    def __init__(self, data, node, lines):
        self.data, self.node, self.lines = data, node, lines

    def get(self, *keys):
        data, node = self.data, self.node
        for key in keys:
            if not isinstance(data, dict) or key not in data:
                return None
            data = data[key]
            node = next(value for name, value in node.value if name.value == key)
        return _Located(data, node, self.lines)

    def line(self, *keys):
        """The verbatim source line declaring the final key, or None."""
        parent = self.get(*keys[:-1]) if keys[:-1] else self
        if parent is None or not isinstance(parent.data, dict) or keys[-1] not in parent.data:
            return None
        key_node = next(name for name, _ in parent.node.value if name.value == keys[-1])
        return self.lines[key_node.start_mark.line].strip()

    def item_lines(self):
        return [self.lines[item.start_mark.line].strip() for item in self.node.value]


def _schema(spec, reference):
    """Resolve a bare local `$ref` object to a named component schema."""
    if not isinstance(reference.data if reference else None, dict) or set(reference.data) != {
        "$ref"
    }:
        return None, None
    match = _REF.fullmatch(str(reference.data["$ref"]))
    name = match[1] if match else None
    schema = spec.get("components", "schemas", name) if name else None
    if schema is None or not isinstance(schema.data, dict):
        return None, None
    return name, schema


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def openapi_claims(text):
    """Return {claim: (value, passages)} for one embedded operation, or None."""
    blocks = list(_FENCE.finditer(text))
    if len(blocks) != 1:
        return None
    block = blocks[0]
    loaded = _load(block["body"])
    if loaded is None:
        return None
    spec = _Located(*loaded, block["body"].splitlines())
    method, path = block["method"], block["path"]
    paths = spec.get("paths")
    operation = spec.get("paths", path, method)
    # The embedded document must describe exactly the operation the page names.
    if (
        paths is None
        or not isinstance(paths.data, dict)
        or set(paths.data) != {path}
        or operation is None
        or not isinstance(operation.data, dict)
        or [key for key in paths.data[path] if key != "parameters"] != [method]
    ):
        return None
    claims = {}

    servers = spec.get("servers")
    url = None
    if servers is not None and isinstance(servers.data, list) and len(servers.data) == 1:
        server = servers.data[0]
        candidate = server.get("url") if isinstance(server, dict) else None
        parts = urlsplit(candidate) if isinstance(candidate, str) else None
        if parts and parts.scheme == "https" and parts.hostname and not parts.query:
            url = candidate.rstrip("/")
    claims["endpoint"] = (
        f"{method.upper()} {url}{path}" if url else None,
        [*servers.item_lines(), spec.line("paths", path)] if url else [],
    )

    request_name, request = _schema(
        spec, operation.get("requestBody", "content", "application/json", "schema")
    )
    fields = request.get("properties") if request else None
    required = request.data.get("required", []) if request else None
    if (
        request is not None
        and request.data.get("type") == "object"
        and fields is not None
        and isinstance(fields.data, dict)
        and fields.data
        and _string_list(required)
        and set(required) <= set(fields.data)
    ):
        schema_line = spec.line("components", "schemas", request_name)
        passages = [schema_line, *(fields.line(name) for name in fields.data)]
        required_passages = [schema_line, *request.get("required").item_lines()]
        claims["request_fields"] = (sorted(fields.data), passages)
        claims["required_fields"] = (sorted(required), required_passages)
        model = fields.get("model")
        if model is not None and isinstance(model.data, dict):
            model_passages = [fields.line("model")]
            if "model" in required:
                model_passages += [
                    line
                    for line, name in zip(
                        request.get("required").item_lines(), required, strict=True
                    )
                    if name == "model"
                ]
            claims["model_required"] = ("model" in required, model_passages)
            # Only an explicit default on an optional field is a default.
            if "model" not in required and "default" in model.data:
                claims["model_default"] = (
                    model.data["default"],
                    [*model_passages, model.line("default")],
                )
        claims["question_types"] = _question_types(spec, fields.get("questions"))

    response_name, response = _schema(
        spec, operation.get("responses", "200", "content", "application/json", "schema")
    )
    answers = response.get("properties", "answers") if response else None
    response_required = response.data.get("required", []) if response else None
    if (
        answers is not None
        and _string_list(response_required)
        and "answers" in response_required
        and schema_sha256(answers.data) == REVIEWED_ANSWERS_SCHEMA_SHA256
    ):
        span = answers.lines[answers.node.start_mark.line : answers.node.end_mark.line]
        claims["response_shape"] = (
            ANSWERS_INTERPRETATION,
            [
                spec.line("components", "schemas", response_name),
                response.get("properties").line("answers"),
                *(line.strip() for line in span if "keyed by" in line),
            ],
        )
    claims["observed_answers_schema_sha256"] = (
        schema_sha256(answers.data) if answers is not None else None
    )
    return claims


def _question_types(spec, questions):
    """Discriminator values whose mapped schemas declare the same `type` constant."""
    unknown = (None, [])
    items = questions.get("additionalProperties") if questions else None
    _, question = _schema(spec, items)
    if question is None:
        return unknown
    discriminator = question.data.get("discriminator")
    mapping = question.get("discriminator", "mapping")
    if (
        not isinstance(discriminator, dict)
        or discriminator.get("propertyName") != "type"
        or mapping is None
        or not isinstance(mapping.data, dict)
        or not mapping.data
        or not isinstance(question.data.get("oneOf"), list)
    ):
        return unknown
    variants = {item.get("$ref") for item in question.data["oneOf"] if isinstance(item, dict)}
    if len(variants) != len(question.data["oneOf"]) or variants != set(mapping.data.values()):
        return unknown
    for name, reference in mapping.data.items():
        if not isinstance(name, str) or not _QUESTION_TYPE.fullmatch(name):
            return unknown
        _, variant = _schema(spec, _Located({"$ref": reference}, None, []))
        properties = variant.data.get("properties") if variant else None
        constant = properties.get("type") if isinstance(properties, dict) else None
        if not isinstance(constant, dict) or constant.get("const") != name:
            return unknown
    return sorted(mapping.data), [mapping.line(name) for name in mapping.data]
