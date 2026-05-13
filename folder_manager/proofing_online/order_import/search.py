from __future__ import annotations

from datetime import timedelta
from typing import List, Optional, Tuple

from .config import SEARCH_WINDOW_DAYS
from .parsers import decode_picture_day_date
from .utils import emit_status


def build_search_queries(
    picture_day_id: str,
    from_address: str,
    use_broad_first: bool = False,
    gmail_label: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """
    Build a list of search queries with descriptions, from most specific to least.
    Returns list of (query, description) tuples.
    """
    queries: List[Tuple[str, str]] = []

    quoted_from = f'"{from_address}"' if '+' in from_address else from_address
    label_filter = f'label:"{gmail_label}"' if gmail_label else ''

    if use_broad_first:
        picture_date = decode_picture_day_date(picture_day_id)

        if picture_date:
            start_date = picture_date - timedelta(days=3)
            query_parts = [f'from:{quoted_from}', f'after:{start_date.strftime("%Y/%m/%d")}']
            if label_filter:
                query_parts.append(label_filter)

            query = ' '.join(query_parts)
            description = f'all messages from sender after picture day ({start_date.strftime("%Y-%m-%d")})'
            if gmail_label:
                description += f' in label "{gmail_label}"'

            emit_status(
                f"Decoded picture day date: {picture_date.strftime('%Y-%m-%d')} from PID {picture_day_id}",
                None,
            )
        else:
            query_parts = [f'from:{quoted_from}', f'newer_than:{SEARCH_WINDOW_DAYS}d']
            if label_filter:
                query_parts.append(label_filter)

            query = ' '.join(query_parts)
            description = f'all messages from sender in last {SEARCH_WINDOW_DAYS} days'
            if gmail_label:
                description += f' in label "{gmail_label}"'

        queries.append((query, description))
        return queries

    base_queries = [
        (picture_day_id, 'Picture Day ID only (will filter by sender after)'),
        (f'"{picture_day_id}"', 'Picture Day ID quoted (will filter by sender after)'),
        (f'{picture_day_id} from:{quoted_from}', 'ID with quoted sender filter'),
    ]

    for base_query, description in base_queries:
        query = f'{base_query} {label_filter}'.strip() if label_filter else base_query
        if gmail_label and gmail_label not in description:
            description += f' in label "{gmail_label}"'
        queries.append((query, description))

    domain = from_address.split('@')[1] if '@' in from_address else from_address
    domain_query = f'{picture_day_id} from:@{domain}'
    if label_filter:
        domain_query += f' {label_filter}'
    domain_description = f'ID with domain filter (@{domain})'
    if gmail_label:
        domain_description += f' in label "{gmail_label}"'
    queries.append((domain_query, domain_description))

    picture_date = decode_picture_day_date(picture_day_id)
    if picture_date:
        start_date = picture_date - timedelta(days=30)
        end_date = picture_date + timedelta(days=30)
        date_query = f'{picture_day_id} after:{start_date.strftime("%Y/%m/%d")} before:{end_date.strftime("%Y/%m/%d")}'
        if label_filter:
            date_query += f' {label_filter}'
        date_description = (
            f'ID with date window ({start_date.strftime("%Y-%m-%d")} to {end_date.strftime("%Y-%m-%d")})'
        )
        if gmail_label:
            date_description += f' in label "{gmail_label}"'
        queries.append((date_query, date_description))

    last_query = f'from:{quoted_from} newer_than:{SEARCH_WINDOW_DAYS}d'
    if label_filter:
        last_query += f' {label_filter}'
    last_description = f'all messages from sender in last {SEARCH_WINDOW_DAYS} days (manual filtering)'
    if gmail_label:
        last_description += f' in label "{gmail_label}"'
    queries.append((last_query, last_description))

    return queries

