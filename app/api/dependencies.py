from typing import cast

from fastapi import Request

from app.resources import Resources


def get_resources(request: Request) -> Resources:
    return cast(Resources, request.app.state.resources)
