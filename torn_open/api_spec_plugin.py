from collections import OrderedDict
from typing import List
from enum import EnumMeta
from functools import wraps
import inspect

from apispec import BasePlugin, APISpec
from apispec.core import Components
from apispec.utils import OpenAPIVersion

from torn_open.types import is_optional, is_list


class TornOpenComponents(Components):
    def schema(self, component_id, component, **kwargs):
        if self.schemas.get(component_id) == component:
            return self

        super().schema(component_id, component, **kwargs)


class TornOpenAPISpec(APISpec):
    def __init__(self, title, version, openapi_version, plugins=(), **options):
        self.title = title
        self.version = version
        self.openapi_version = OpenAPIVersion(openapi_version)
        self.options = options
        self.plugins = plugins

        # Metadata
        self._tags = []
        self._paths = OrderedDict()

        # Components
        self.components = TornOpenComponents(self.plugins, self.openapi_version)

        # Plugins
        for plugin in self.plugins:
            plugin.init_spec(self)

class TornOpenPlugin(BasePlugin):
    """APISpec plugin for Tornado"""

    def init_spec(self, spec):
        self.spec = spec

    def path_helper(self, *, url_spec, parameters, **_):
        path = get_path(url_spec)
        parameters.extend(get_path_params(url_spec))
        return path

    def operation_helper(self, *, operations, url_spec, **_):
        operations.update(**Operations(url_spec, self.spec.components))


# Path helper methods
def get_path(url_spec):
    path = replace_path_with_openapi_placeholders(url_spec)
    path = right_strip_path(path)
    return path


def extract_and_sort_path_path_params(url_spec):
    path_params = url_spec.regex.groupindex
    path_params = {
        k: v for k, v in sorted(path_params.items(), key=lambda item: item[1])
    }
    path_params = tuple(f"{{{param}}}" for param in path_params)
    return path_params


def replace_path_with_openapi_placeholders(url_spec):
    path = url_spec.matcher._path
    if url_spec.regex.groups == 0:
        return path

    path_params = extract_and_sort_path_path_params(url_spec)
    return path % path_params


def right_strip_path(path):
    return path.rstrip("/*")


# Path params
def get_path_params(url_spec):
    handler = url_spec.handler_class
    path_params = handler.path_params
    parameters = [PathParameter(parameter) for parameter in path_params.values()]
    return parameters


def _unpack_enum(enum_meta: EnumMeta):
    for enum_item in enum_meta.__members__.values():
        yield enum_item.value


def _get_type_of_enum_value(enum_meta: EnumMeta):
    for enum_item in _unpack_enum(enum_meta):
        return type(enum_item)


def _get_default_value_of_parameter(parameter: inspect.Parameter):
    annotation = parameter.annotation
    default = parameter.default if parameter.default is not inspect._empty else None
    return default.value if default and isinstance(annotation, EnumMeta) else default


def _get_type(annotation):
    types_mapping = {
        str: "string",
        int: "integer",
        float: "number",
        list: "array",
        List: "array",
    }
    if annotation in types_mapping:
        return types_mapping[annotation]

    if is_optional(annotation):
        return _get_type(annotation.__args__[0])

    if isinstance(annotation, EnumMeta):
        annotation = _get_type_of_enum_value(annotation)
        return _get_type(annotation)

    if is_list(annotation):
        annotation = annotation.__origin__
        return _get_type(annotation)

    if not isinstance(annotation, type) and is_optional(annotation.__args__):
        return _get_type(annotation.__args__[0])

def _get_item_type(annotation):
    if len(annotation.__args__) == 1:
        item_type = _get_type(annotation.__args__[0])
    elif is_optional(annotation):
        item_type = _get_type(annotation.__args__[0].__args__[0])
    else:
        item_type = None
    return item_type


def Items(annotation):
    if _get_type(annotation) != "array":
        return None

    item_type = _get_item_type(annotation)

    return {
        "type": item_type,
    }


def _get_type_of_optional_array(annotation):
    return _get_type(annotation.__args__[0].__args__[0])


def Schema(parameter: inspect.Parameter):
    annotation = parameter.annotation
    _type = _get_type(annotation)
    _enum = [*_unpack_enum(annotation)] if isinstance(annotation, EnumMeta) else None
    default = _get_default_value_of_parameter(parameter)
    items = Items(annotation)

    schema = {
        "type": _type,
        "enum": _enum,
        "default": default,
        "items": items,
    }
    schema = _clear_none_from_dict(schema)
    return schema


def PathParameter(parameter: inspect.Parameter):
    return Parameter(parameter, param_type="path", required=True)


def QueryParameter(parameter: inspect.Parameter):
    return Parameter(parameter, param_type="query")


def Parameter(parameter: inspect.Parameter, param_type, required: bool = None):
    return {
        "name": parameter.name,
        "in": param_type,
        "required": required
        if required is not None
        else not is_optional(parameter.annotation),
        "schema": Schema(parameter),
    }


# Operations helper methods
def Operations(url_spec, components):
    implemented_methods = _get_implemented_http_methods(url_spec)
    operations = {
        method: Operation(method, url_spec, components)
        for method in implemented_methods
    }
    return operations


def Operation(method: str, url_spec, components):
    operation = {
        "tags": _get_tags(method, url_spec),
        "summary": _get_summary(method, url_spec),
        "parameters": _get_query_params(method, url_spec),
        "description": _get_operation_description(method, url_spec),
        "requestBody": RequestBody(method, url_spec),
        "responses": Responses(method, url_spec, components),
    }
    operation = _clear_none_from_dict(operation)
    return operation

def _get_tags(method, url_spec):
    handler = url_spec.handler_class
    method = getattr(handler, method, None)
    if method is handler._unimplemented_method:
        return None
    return getattr(method, "_openapi_tags", None)

def _get_summary(method, url_spec):
    handler = url_spec.handler_class
    method = getattr(handler, method, None)
    if method is handler._unimplemented_method:
        return None
    return getattr(method, "_openapi_summary", None)

def _get_operation_description(method: str, url_spec):
    handler = url_spec.handler_class
    description = getattr(handler, method).__doc__
    description = description.strip() if description else description
    return description


def _get_query_params(method, url_spec):
    handler = url_spec.handler_class
    parameters = handler.query_params[method].values()
    return [QueryParameter(parameter) for parameter in parameters]


def _get_implemented_http_methods(url_spec):
    handler = url_spec.handler_class
    return [
        method.lower()
        for method in handler.SUPPORTED_METHODS
        if _is_implemented(method.lower(), handler)
    ]


SCHEMA_REF_TEMPLATE = "#/components/schemas/{model}"


def RequestBody(method: str, url_spec):
    handler = url_spec.handler_class
    json_param = handler.json_param[method]
    if not json_param:
        return None
    _, parameter = json_param
    return {"content": {"application/json": {"schema": RequestBodySchema(parameter)}}}


def RequestBodySchema(parameter):
    return parameter.annotation.schema(ref_template=SCHEMA_REF_TEMPLATE)


def Responses(method, url_spec, components):
    return {"200": SuccessResponse(method, url_spec, components)}


def SuccessResponse(method, url_spec, components):
    response_model = url_spec.handler_class.response_models[method]
    return {
        "description": get_success_response_description(response_model),
        "content": {
            "application/json": {
                "schema": SuccessResponseModelSchema(response_model, components)
            }
        },
    }


def get_success_response_description(response_model):
    TEMPLATE_RESPONSE_DESCRIPTION = '''
    Include a `torn_open.models.ResponseModel` annotation with documentation to overwrite this default description.

    Example
    ```python
    from torn_open.web import AnnotatedHandler
    from torn_open.models import ResponseModel

    class MyResponseModel(ResponseModel):
        """
        Successfully retrieved my response model
        """
        spam: str
        ham: int

    class MyHandler(AnnotatedHandler):
        async def get(self) -> MyResponseModel:
            pass

    ```
    '''
    description = TEMPLATE_RESPONSE_DESCRIPTION.strip()
    if response_model and response_model.__doc__:
        description = response_model.__doc__.strip()
    return description


def SuccessResponseModelSchema(response_model, components):
    schema = (
        response_model.schema(ref_template=SCHEMA_REF_TEMPLATE)
        if response_model
        else None
    )
    if not schema:
        return schema

    referenced_schemas = schema.pop("definitions", {})
    if not referenced_schemas:
        return schema

    for referenced_schema_id, referenced_schema in referenced_schemas.items():
        components.schema(referenced_schema_id, referenced_schema)
    return schema


def tags(*tag_list):
    def decorator(func):
        func._openapi_tags = [*tag_list]

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator

def summary(summary_text):
    def decorator(func):
        func._openapi_summary = summary_text

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator

def _is_implemented(method, handler):
    return getattr(handler, method) is not handler._unimplemented_method


def _clear_none_from_dict(dictionary):
    return {k: v for k, v in dictionary.items() if v}
