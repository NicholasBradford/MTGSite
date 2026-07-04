from functools import wraps
from flask import abort, current_app


def feature_enabled(feature_name: str) -> bool:
    manager = current_app.extensions["settings_manager"]
    return manager.feature_enabled(feature_name)


def require_feature(feature_name: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            manager = current_app.extensions["settings_manager"]

            if not manager.feature_enabled(feature_name):
                abort(404)

            return fn(*args, **kwargs)

        return wrapper

    return decorator