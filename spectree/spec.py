from collections import defaultdict
from copy import deepcopy
from functools import wraps

from .config import Config
from .models import Tag, ValidationError
from .plugins import PLUGINS
from .utils import (
    default_after_handler,
    default_before_handler,
    get_model_key,
    get_model_schema,
    get_security,
    parse_comments,
    parse_name,
    parse_params,
    parse_request,
    parse_resp,
)


class SpecTree:
    """
    Interface

    :param str backend_name: choose from ('flask', 'falcon', 'falcon-asgi', 'starlette')
    :param backend: a backend that inherit `SpecTree.plugins.base.BasePlugin`
    :param app: backend framework application instance (can be registered later)
    :param before: a callback function of the form
        :meth:`spectree.utils.default_before_handler`
        ``func(req, resp, req_validation_error, instance)``
        that will be called after the request validation before the endpoint function
    :param after: a callback function of the form
        :meth:`spectree.utils.default_after_handler`
        ``func(req, resp, resp_validation_error, instance)``
        that will be called after the response validation
    :param validation_error_status: The default response status code to use in the
        event of a validation error. This value can be overridden for specific endpoints
        if needed.
    :param kwargs: update default :class:`spectree.config.Config`
    """

    def __init__(
        self,
        backend_name="base",
        backend=None,
        app=None,
        before=default_before_handler,
        after=default_after_handler,
        validation_error_status=422,
        **kwargs,
    ):
        self.before = before
        self.after = after
        self.validation_error_status = validation_error_status
        self.config = Config(**kwargs)
        self.backend_name = backend_name
        self.backend = backend(self) if backend else PLUGINS[backend_name](self)
        # init
        self.models = {}
        if app:
            self.register(app)

    def register(self, app):
        """
        register to backend application

        This will be automatically triggered if the app is passed into the
        init step.
        """
        self.app = app
        self.backend.register_route(self.app)

    @property
    def spec(self):
        """
        get the OpenAPI spec
        """
        if not hasattr(self, "_spec"):
            self._spec = self._generate_spec()
        return self._spec

    def bypass(self, func):
        """
        bypass rules for routes (mode defined in config)

        :normal:    collect all the routes exclude those decorated by other
                    `SpecTree` instance
        :greedy:    collect all the routes
        :strict:    collect all the routes decorated by this instance
        """
        if self.config.MODE == "greedy":
            return False
        elif self.config.MODE == "strict":
            return getattr(func, "_decorator", None) != self
        else:
            decorator = getattr(func, "_decorator", None)
            return bool(decorator and decorator != self)

    def validate(
        self,
        query=None,
        json=None,
        headers=None,
        cookies=None,
        resp=None,
        tags=(),
        security=None,
        deprecated=False,
        before=None,
        after=None,
        validation_error_status=None,
        path_parameter_descriptions=None,
    ):
        """
        - validate query, json, headers in request
        - validate response body and status code
        - add tags to this API route
        - add security to this API route

        :param query: `pydantic.BaseModel`, query in uri like `?name=value`
        :param json: `pydantic.BaseModel`, JSON format request body
        :param headers: `pydantic.BaseModel`, if you have specific headers
        :param cookies: `pydantic.BaseModel`, if you have cookies for this route
        :param resp: `spectree.Response`
        :param tags: a tuple of strings or :class:`spectree.models.Tag`
        :param security: dict with security config for current route and method
        :param deprecated: bool if endpoint is marked as deprecated
        :param before: :meth:`spectree.utils.default_before_handler` for
            specific endpoint
        :param after: :meth:`spectree.utils.default_after_handler` for
            specific endpoint
        :param validation_error_status: The response status code to use for the
            specific endpoint, in the event of a validation error. If not specified,
            the global `validation_error_status` is used instead, defined
            in :meth:`spectree.spec.SpecTree`.
        :param path_parameter_descriptions: A dictionary of path parameter names and
            their description.
        """
        # If the status code for validation errors is not overridden on the level of
        # the view function, use the globally set status code for validation errors.
        if not validation_error_status:
            validation_error_status = self.validation_error_status

        def decorate_validation(func):
            # for sync framework
            @wraps(func)
            def sync_validate(*args, **kwargs):
                return self.backend.validate(
                    func,
                    query,
                    json,
                    headers,
                    cookies,
                    resp,
                    before or self.before,
                    after or self.after,
                    validation_error_status,
                    *args,
                    **kwargs,
                )

            # for async framework
            @wraps(func)
            async def async_validate(*args, **kwargs):
                return await self.backend.validate(
                    func,
                    query,
                    json,
                    headers,
                    cookies,
                    resp,
                    before or self.before,
                    after or self.after,
                    validation_error_status,
                    *args,
                    **kwargs,
                )

            validation = async_validate if self.backend.ASYNC else sync_validate

            if self.config.ANNOTATIONS:
                nonlocal query
                query = func.__annotations__.get("query", query)
                nonlocal json
                json = func.__annotations__.get("json", json)
                nonlocal headers
                headers = func.__annotations__.get("headers", headers)
                nonlocal cookies
                cookies = func.__annotations__.get("cookies", cookies)

            # register
            for name, model in zip(
                ("query", "json", "headers", "cookies"), (query, json, headers, cookies)
            ):
                if model is not None:
                    model_key = self._add_model(model=model)
                    setattr(validation, name, model_key)

            if resp:
                # Make sure that the endpoint specific status code and data model for
                # validation errors shows up in the response spec.
                resp.add_model(validation_error_status, ValidationError, replace=False)
                for model in resp.models:
                    self._add_model(model=model)
                validation.resp = resp

            if tags:
                validation.tags = tags

            validation.security = security
            validation.deprecated = deprecated
            validation.path_parameter_descriptions = path_parameter_descriptions
            # register decorator
            validation._decorator = self
            return validation

        return decorate_validation

    def _add_model(self, model) -> str:
        """
        unified model processing
        """

        model_key = get_model_key(model=model)
        self.models[model_key] = deepcopy(get_model_schema(model=model))

        return model_key

    def _generate_spec(self):
        """
        generate OpenAPI spec according to routes and decorators
        """
        routes = defaultdict(dict)
        tags = {}
        for route in self.backend.find_routes():
            for method, func in self.backend.parse_func(route):
                path_parameter_descriptions = getattr(
                    func, "path_parameter_descriptions", None
                )
                path, parameters = self.backend.parse_path(
                    route, path_parameter_descriptions
                )

                if self.backend.bypass(func, method) or self.bypass(func):
                    continue

                name = parse_name(func)
                summary, desc = parse_comments(func)
                func_tags = getattr(func, "tags", ())
                for tag in func_tags:
                    if str(tag) not in tags:
                        tags[str(tag)] = (
                            tag.dict() if isinstance(tag, Tag) else {"name": tag}
                        )

                routes[path][method.lower()] = {
                    "summary": summary or f"{name} <{method}>",
                    "operationId": f"{method.lower()}_{path}",
                    "description": desc or "",
                    "tags": [str(x) for x in getattr(func, "tags", ())],
                    "parameters": parse_params(func, parameters[:], self.models),
                    "responses": parse_resp(func),
                }

                security = getattr(func, "security", None)
                if security is not None:
                    routes[path][method.lower()]["security"] = get_security(security)

                deprecated = getattr(func, "deprecated", False)
                if deprecated:
                    routes[path][method.lower()]["deprecated"] = deprecated

                request_body = parse_request(func)
                if request_body:
                    routes[path][method.lower()]["requestBody"] = request_body

        spec = {
            "openapi": self.config.OPENAPI_VERSION,
            "info": {
                "title": self.config.TITLE,
                "version": self.config.VERSION,
                "description": self.config.DESCRIPTION,
            },
            "tags": list(tags.values()),
            "paths": {**routes},
            "components": {
                "schemas": {**self.models, **self._get_model_definitions()},
            },
        }

        if self.config.SERVERS:
            spec["servers"] = [
                server.dict(exclude_none=True) for server in self.config.SERVERS
            ]

        if self.config.SECURITY_SCHEMES:
            spec["components"]["securitySchemes"] = {
                scheme.name: scheme.data.dict(exclude_none=True, by_alias=True)
                for scheme in self.config.SECURITY_SCHEMES
            }

        spec["security"] = get_security(self.config.SECURITY)
        return spec

    def _get_model_definitions(self):
        """
        handle nested models
        """
        definitions = {}
        for name, schema in self.models.items():
            if "definitions" in schema:
                for key, value in schema["definitions"].items():
                    definitions[f"{name}.{key}"] = value
                del schema["definitions"]

        return definitions
