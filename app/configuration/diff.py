"""Read-only comparison of typed documents; ordered collections are compared atomically."""

from typing import cast

from pydantic import JsonValue

from app.configuration.schemas import ConfigurationChange, OperationalConfiguration


def configuration_changes(
    published: OperationalConfiguration | None, draft: OperationalConfiguration
) -> list[ConfigurationChange]:
    changes: list[ConfigurationChange] = []

    def compare(before: JsonValue, after: JsonValue, field: str, *, initial: bool) -> None:
        if isinstance(after, dict) and (initial or isinstance(before, dict)) and after:
            before_keys: set[str] = set(before) if isinstance(before, dict) else set()
            for key in sorted(before_keys | set(after)):
                path = f"{field}.{key}" if field else key
                if key not in after:
                    changes.append(
                        ConfigurationChange(
                            field=path,
                            kind="removed",
                            published=before[key] if isinstance(before, dict) else None,
                            draft=None,
                        )
                    )
                    continue
                compare(
                    before.get(key) if isinstance(before, dict) else None,
                    after[key],
                    path,
                    initial=initial or key not in before_keys,
                )
        elif initial or before != after:
            changes.append(
                ConfigurationChange(
                    field=field,
                    kind="added" if initial else "changed",
                    published=before,
                    draft=after,
                )
            )

    compare(
        cast(JsonValue, published.model_dump(mode="json")) if published is not None else None,
        cast(JsonValue, draft.model_dump(mode="json")),
        "",
        initial=published is None,
    )
    return changes
