"""Closed favorite command contract shared by native request dispatch."""

from internal.application.errors import ApplicationError


def valid_entry_ids(values: list) -> bool:
    return (
        1 <= len(values) <= 100
        and all(type(value) is str and 0 < len(value) <= 64 for value in values)
        and len(set(values)) == len(values)
    )

METHODS = frozenset({
    "favorites.list", "favorites.get", "favorites.add",
    "favorites.update", "favorites.reorder", "favorites.delete",
    "favorites.copy", "favorites.batch_add", "favorites.export",
    "favorites.group_create", "favorites.group_rename", "favorites.group_delete",
})


def valid_order(favorite_ids: list) -> bool:
    """A list of distinct favourite ids: the ones being moved, in their order."""
    return (
        1 <= len(favorite_ids) <= 100
        and all(type(value) is str and 0 < len(value) <= 64 for value in favorite_ids)
        and len(set(favorite_ids)) == len(favorite_ids)
    )


def dispatch_favorites(app, method, params, validate):
    identifier = {"favorite_id": (str, lambda v: 0 < len(v) <= 64)}
    group_name = {"name": (str, lambda v: 0 < len(v) <= 128 and bool(v.strip()))}
    text_fields = {
        "title": (str, lambda v: len(v) <= 256),
        "content": (str, lambda v: len(v) <= 65536),
        "group": (str, lambda v: len(v) <= 128),
    }
    if method == "favorites.list":
        validate(params, {
            "query": (str, lambda v: len(v) <= 512),
            "group": text_fields["group"],
            "offset": (int, lambda v: 0 <= v <= 1000000),
            "limit": (int, lambda v: 1 <= v <= 100),
        })
        service = app.require_favorites()
        return app.events.snapshot(lambda: service.list(**params))
    if method == "favorites.batch_add":
        validate(params, {
            "entry_ids": (list, valid_entry_ids),
            "group": text_fields["group"],
        }, ("entry_ids", "group"))
        result = app.require_favorites().batch_add(**params)
    elif method == "favorites.export":
        validate(params, {
            "format": (str, lambda v: v in ("markdown", "text")),
        }, ("format",))
        # An export writes a file but does not change stored favourites, so it
        # must not publish favorites.changed.
        return app.require_favorites().export(**params)
    elif method == "favorites.add":
        validate(params, text_fields, ("title", "content", "group"))
        result = app.require_favorites().add(**params)
    elif method == "favorites.update":
        validate(
            params,
            {**identifier, **text_fields, "position": (int, lambda v: 0 <= v <= 1000000)},
            ("favorite_id", "title", "content", "group", "position"),
        )
        result = app.require_favorites().update(**params)
    elif method == "favorites.reorder":
        validate(params, {"favorite_ids": (list, valid_order)}, ("favorite_ids",))
        result = app.require_favorites().reorder(params["favorite_ids"])
    elif method == "favorites.group_create":
        validate(params, group_name, ("name",))
        result = app.require_favorites().create_group(**params)
    elif method == "favorites.group_rename":
        validate(params, {**group_name, "rename_to": group_name["name"]}, ("name", "rename_to"))
        result = app.require_favorites().rename_group(params["name"], params["rename_to"])
    elif method == "favorites.group_delete":
        validate(params, group_name, ("name",))
        result = app.require_favorites().delete_group(**params)
    else:
        validate(params, identifier, ("favorite_id",))
        service = app.require_favorites()
        if method == "favorites.get":
            return service.get(**params)
        if method == "favorites.copy":
            return service.copy(**params)
        if method != "favorites.delete":
            raise ApplicationError("METHOD_NOT_FOUND", "Unknown method")
        result = service.delete(**params)
    app.events.publish("favorites.changed", {})
    return result
