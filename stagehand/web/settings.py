from __future__ import annotations

import os


def rename_example(tvdir: str, separator: str, season_dir_format: str, code_style: str,
                   episode_format: str) -> dict[str, str]:
    """
    Returns an example path for an episode given the naming settings, or an
    error if the settings are invalid.
    """
    show, title, season, ep = 'Dead Like Me', 'The Shallow End', 2, 4
    try:
        example = os.path.join(
            tvdir,
            season_dir_format.format(season=season),
            episode_format.format(
                show=show.replace(' ', separator),
                code=code_style.format(season=season, episode=ep),
                title=title.replace(' ', separator),
            ),
        )
    except KeyError as exc:
        return {'error': f'{exc.args[0]} is not a valid identifier'}
    except (ValueError, IndexError) as exc:
        return {'error': str(exc.args[0])}
    return {'example': example}
