# ruff: noqa: E501, S106, PLR0915, RUF005 - a build script: long markdown f-strings, a dummy secret, one
# long render() routine, and list concatenation that reads more clearly than unpacking.
"""Generate the frontend hand-off docs from the code (no database, no network). Run: `make frontend-docs`.

Builds the same routers the API process mounts, exports OpenAPI 3.1, and writes, into docs/frontend/:
* CRM_API.md / WEBSITE_API.md = the hand-written head (scripts/frontend_heads/) + a reference generated per
  endpoint: method + path, purpose (route docstring), who may call it (read from the route's require_role /
  require_human_mfa dependency closures), parameters, request body and response fields;
* openapi-crm.json / openapi-website.json for a mock server.
CMS.md is hand-written and is not touched here.
"""

import json
import pathlib
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from kx.agents.website import WebsiteDeps
from kx.agents.website import build_router as website_router
from kx.automation.admin_api import build_router as channels_admin_router
from kx.core.auth import Role
from kx.core.errors import install_problem_handlers
from kx.crm.api import build_router as crm_router
from kx.crm.deps import CrmDeps
from kx.crm.pdf import Letterhead

HERE = pathlib.Path(__file__).resolve().parent
HEADS = HERE / "frontend_heads"
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "docs" / "frontend"
OUT.mkdir(parents=True, exist_ok=True)


class _Dummy:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("offline build")


class _Dbs(_Dummy):
    def dependency(self, db: Any) -> Any:
        async def session() -> Any:  # never called: the app is only built to export OpenAPI
            yield None

        return session


def crm_app() -> FastAPI:
    app = FastAPI(title="Kairox CRM API", version="1.0.0")
    install_problem_handlers(app)
    deps = CrmDeps(uploader=_Dummy(), letterhead=Letterhead(name="Arifaa Pack", address=""))  # type: ignore[arg-type]
    app.include_router(crm_router(_Dbs(), deps), prefix="/v1")  # type: ignore[arg-type]
    app.include_router(channels_admin_router(_Dummy()))  # type: ignore[arg-type]

    @app.get("/.well-known/jwks.json", tags=["auth"])
    async def jwks() -> dict[str, Any]:
        """The public key that verifies staff access tokens (RS256, D26)."""
        return {}

    return app


def website_app() -> FastAPI:
    app = FastAPI(title="Kairox website API", version="1.0.0")
    install_problem_handlers(app)
    deps = WebsiteDeps(
        databases=_Dummy(),  # type: ignore[arg-type]
        service=_Dummy(),  # type: ignore[arg-type]
        gateway=_Dummy(),  # type: ignore[arg-type]
        secret="x",
        human_check=None,
        limits=_Dummy(),  # type: ignore[arg-type]
        ai_budget_left=_Dummy(),  # type: ignore[arg-type]
    )
    app.include_router(website_router(deps))
    return app


def flat_routes(items: Any, prefix: str = "") -> list[tuple[str, APIRoute]]:
    """FastAPI 0.16x keeps included routers nested (`_IncludedRouter`): walk them, adding each prefix."""
    out: list[tuple[str, APIRoute]] = []
    for item in items:
        if isinstance(item, APIRoute):
            out.append((prefix + item.path, item))
        elif hasattr(item, "original_router"):
            out += flat_routes(item.original_router.routes, prefix + item.include_context.prefix)
    return out


def who(route: APIRoute) -> str:
    """Roles from the auth dependency closures (MD passes wherever MANAGER is listed, kx.core.auth)."""
    found: list[str] = []

    def visit(dep: Any) -> None:
        call = getattr(dep, "call", None)
        qual = getattr(call, "__qualname__", "")
        if qual.startswith(("require_role.", "require_human_mfa.")):
            roles: tuple[Role, ...] = ()
            for cell in call.__closure__ or ():
                value = cell.cell_contents
                if isinstance(value, tuple) and all(isinstance(r, Role) for r in value):
                    roles = value
            names = [r.value for r in roles]
            if "manager" in names and "md" not in names:
                names.append("md")
            text = ", ".join(names) if names else "any staff"
            if qual.startswith("require_human_mfa."):
                text += " (signed in with the authenticator app: `mfa=true`)"
            found.append(text)
        elif qual == "current_principal":
            found.append("any signed-in staff")
        for sub in getattr(dep, "dependencies", []):
            visit(sub)

    visit(route.dependant)
    specific = [f for f in found if f != "any signed-in staff"]
    return "; ".join(dict.fromkeys(specific or found)) or "public"


def _type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return "`" + schema["$ref"].rsplit("/", 1)[-1] + "`"
    if "anyOf" in schema:
        return " or ".join(_type(s) for s in schema["anyOf"])
    if "enum" in schema:
        return " \\| ".join(f"`{v}`" for v in schema["enum"])
    if "const" in schema:
        return f"`{schema['const']}`"
    t = schema.get("type", "any")
    if t == "array":
        return f"list of {_type(schema.get('items', {}))}"
    if (
        t == "object"
        and "additionalProperties" in schema
        and isinstance(schema["additionalProperties"], dict)
    ):
        return f"map of {_type(schema['additionalProperties'])}"
    fmt = schema.get("format")
    return f"{t} ({fmt})" if fmt else str(t)


def _limits(schema: dict[str, Any]) -> str:
    bits = []
    for k, label in (
        ("minLength", "min length"),
        ("maxLength", "max length"),
        ("minimum", "≥"),
        ("exclusiveMinimum", ">"),
        ("maximum", "≤"),
        ("pattern", "pattern"),
        ("minItems", "min items"),
        ("maxItems", "max items"),
    ):
        if k in schema:
            bits.append(f"{label} `{schema[k]}`" if k == "pattern" else f"{label} {schema[k]}")
    if "default" in schema:
        bits.append(f"default `{json.dumps(schema['default'])}`")
    return "; ".join(bits)


def fields_table(schema: dict[str, Any]) -> list[str]:
    props = schema.get("properties", {})
    if not props:
        return ["(no fields)"]
    required = set(schema.get("required", []))
    rows = ["| Field | Type | Required | Notes |", "|---|---|---|---|"]
    for name, s in props.items():
        notes = " ".join(x for x in (s.get("description", ""), _limits(s)) if x)
        rows.append(f"| `{name}` | {_type(s)} | {'yes' if name in required else 'no'} | {notes} |")
    return rows


def render(
    app: FastAPI, title: str, prefix_filter: tuple[str, ...], exclude: tuple[str, ...] = ()
) -> tuple[str, dict[str, Any]]:
    spec = app.openapi()
    comps = spec.get("components", {}).get("schemas", {})
    routes = {(path, m): r for path, r in flat_routes(app.routes) for m in r.methods}
    lines = [f"## {title}", ""]
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, ops in spec["paths"].items():
        if not path.startswith(prefix_filter) or path.startswith(exclude):
            continue
        for method, op in ops.items():
            tag = (op.get("tags") or ["other"])[0]
            by_tag.setdefault(tag, []).append((method.upper(), path, op))
    used: set[str] = set()
    for tag in sorted(by_tag):
        lines += [f"### {tag}", ""]
        for method, path, op in by_tag[tag]:
            route = routes.get((path, method))
            lines += [f"#### `{method} {path}`", ""]
            doc = (route.description if route else op.get("description", "")) or op.get("summary", "")
            if doc:
                lines += [" ".join(doc.split()), ""]
            caller = who(route) if route else "public"
            if caller == "public" and any(
                q.get("in") == "header" and q["name"] == "authorization" for q in op.get("parameters", [])
            ):
                caller = "the website session: header `Authorization: Bearer <session_token>`"
            lines += [f"**Who:** {caller}", ""]
            params = [
                p for p in op.get("parameters", []) if p.get("in") != "header" or p["name"] != "authorization"
            ]
            if params:
                lines += [
                    "**Parameters**",
                    "",
                    "| Name | In | Type | Required | Notes |",
                    "|---|---|---|---|---|",
                ]
                for p in params:
                    s = p.get("schema", {})
                    lines.append(
                        f"| `{p['name']}` | {p['in']} | {_type(s)} | {'yes' if p.get('required') else 'no'} | {_limits(s)} |"
                    )
                lines.append("")
            body = op.get("requestBody", {}).get("content", {})
            for ctype, media in body.items():
                ref = media.get("schema", {}).get("$ref")
                name = ref.rsplit("/", 1)[-1] if ref else None
                lines += [
                    f"**Request body** (`{ctype}`): "
                    + (f"`{name}`" if name else _type(media.get("schema", {}))),
                    "",
                ]
                if name:
                    used.add(name)
                    lines += fields_table(comps[name]) + [""]
            for code, resp in op.get("responses", {}).items():
                if code == "422" and "HTTPValidationError" in json.dumps(resp):
                    continue
                content = resp.get("content", {})
                schema = next(iter(content.values()), {}).get("schema", {}) if content else {}
                shown = _type(schema) if schema else "(no body)"
                lines.append(f"**Response {code}:** {shown}")
                for ref in _refs(schema):
                    used.add(ref)
            lines += ["", "---", ""]
    # schemas appendix: everything referenced, transitively
    pending = list(used)
    while pending:
        name = pending.pop()
        for ref in _refs(comps.get(name, {})):
            if ref not in used:
                used.add(ref)
                pending.append(ref)
    lines += [f"## {title}: data shapes", ""]
    for name in sorted(used):
        if name in ("HTTPValidationError", "ValidationError"):
            continue
        schema = comps[name]
        lines += [f"### `{name}`", ""]
        if schema.get("description"):
            lines += [" ".join(schema["description"].split()), ""]
        if "enum" in schema:
            lines += ["One of: " + ", ".join(f"`{v}`" for v in schema["enum"]), ""]
        else:
            lines += fields_table(schema) + [""]
    return "\n".join(lines), spec


def _refs(schema: Any) -> list[str]:
    out: list[str] = []
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k == "$ref" and isinstance(v, str):
                out.append(v.rsplit("/", 1)[-1])
            else:
                out += _refs(v)
    elif isinstance(schema, list):
        for v in schema:
            out += _refs(v)
    return out


crm_md, crm_spec = render(crm_app(), "CRM endpoint reference", ("/v1", "/.well-known"))
web_md, web_spec = render(website_app(), "Website endpoint reference", ("/v1/public",))


def _doc(head: str, body: str) -> str:
    return (HEADS / head).read_text(encoding="utf-8").rstrip() + "\n\n" + body + "\n"


(OUT / "CRM_API.md").write_text(_doc("crm_head.md", crm_md), encoding="utf-8")
(OUT / "WEBSITE_API.md").write_text(_doc("web_head.md", web_md), encoding="utf-8")
(OUT / "openapi-crm.json").write_text(json.dumps(crm_spec, indent=2), encoding="utf-8")
(OUT / "openapi-website.json").write_text(json.dumps(web_spec, indent=2), encoding="utf-8")
print("CRM_API.md:", len(crm_spec["paths"]), "paths; WEBSITE_API.md:", len(web_spec["paths"]), "paths")
