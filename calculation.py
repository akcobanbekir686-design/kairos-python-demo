"""KAIROS pure calculation slice. No persistence, model or delivery authority.
Source: Kairos.docx 2026-10-31, sections 8–10.
Ranked pools must be produced by a trusted coordinator; A scores are not invented.
"""
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

VERSION = 'kairos-selection-0.1'
CHANNELS = (('A', 12, 3), ('symbol', 6, 1), ('recent', 3, 1))

@dataclass(frozen=True)
class Record:
    source_id: str
    owner: str
    session_id: str
    symbols: tuple = ()
    origin: str = 'user_narrative'
    status: str = 'active'
    session_state: str = 'completed'
    event_group: Optional[str] = None
    entertainment: bool = False


def eligible(r, owner, current_session):
    return (r.owner == owner and r.session_id != current_session
            and r.origin == 'user_narrative' and r.status == 'active'
            and r.session_state == 'completed' and not r.entertainment)


def symbol_ids(values):
    if not isinstance(values, (tuple, list, set, frozenset)):
        raise ValueError('symbol_id_collection_required')
    if any(not isinstance(s, str) or not s or s != s.strip() for s in values):
        raise ValueError('canonical_symbol_id_required')
    return set(values)


def repetition(current_symbols, records, *, owner, current_session):
    """None = unavailable; [] = no valid symbols; valid nonmatch = real zero.
    Input contains canonical narrative symbols only, never tarot draw notices.
    No synonym inference or symbol extraction is performed here.
    """
    base = dict(method=VERSION, source_ids=[])
    if current_symbols is None:
        return dict(base, state='unavailable', value=None, numerator=None, denominator=None)
    symbols = symbol_ids(current_symbols)
    if not symbols:
        return dict(base, state='empty', value=None, numerator=0, denominator=0)
    evidence = {}
    seen = {}
    for r in records:
        if r.source_id in seen and seen[r.source_id] != r:
            raise ValueError('conflicting_source_snapshot')
        seen[r.source_id] = r
        if not eligible(r, owner, current_session):
            continue
        for sym in symbol_ids(r.symbols) & symbols:
            evidence.setdefault(sym, set()).add(r.source_id)
    return dict(base, state='calculated', value=Fraction(len(evidence), len(symbols)),
                numerator=len(evidence), denominator=len(symbols),
                source_ids=sorted({sid for ids in evidence.values() for sid in ids}),
                evidence={sym: sorted(ids) for sym, ids in sorted(evidence.items())})


def select(pools, *, owner, current_session):
    """Accept explicitly ranked pools. Dedupe/filter before 12/6/3 caps.
    Stable input order is the ranking; absent score/time logic is not synthesized.
    Full event-group identity should be normalized upstream.
    """
    if set(pools) != {c for c, _, _ in CHANNELS}:
        raise ValueError('three_ranked_channels_required')
    snapshots, prepared, audit = {}, {}, []
    for channel, cap, _ in CHANNELS:
        prepared[channel] = []
        channel_seen = set()
        for rank, r in enumerate(pools[channel], 1):
            if not isinstance(r, Record) or not r.source_id:
                raise ValueError('source_record_required')
            if r.source_id in snapshots and snapshots[r.source_id] != r:
                raise ValueError('conflicting_source_snapshot')
            snapshots[r.source_id] = r
            reason = None
            if not eligible(r, owner, current_session): reason = 'ineligible'
            elif r.source_id in channel_seen: reason = 'duplicate_in_pool'
            elif len(prepared[channel]) >= cap: reason = 'pool_cap'
            channel_seen.add(r.source_id)
            if reason:
                audit.append(dict(source_id=r.source_id, channel=channel, rank=rank, reason=reason))
            else:
                prepared[channel].append((rank, r))
    membership = {}
    for c, _, _ in CHANNELS:
        for _, r in prepared[c]: membership.setdefault(r.source_id, []).append(c)
    chosen, groups, selected_ids = [], {}, set()
    for c, _, quota in CHANNELS:
        used = 0
        for rank, r in prepared[c]:
            if r.source_id in selected_ids: reason = 'already_selected'
            elif r.event_group is not None and groups.get(r.event_group, 0) >= 2: reason = 'event_group_cap'
            elif used >= quota: reason = 'channel_quota'
            else:
                reason = 'selected'
                chosen.append(dict(source_id=r.source_id, selected_by=c, matched_channels=membership[r.source_id]))
                selected_ids.add(r.source_id); used += 1
                if r.event_group is not None: groups[r.event_group] = groups.get(r.event_group, 0) + 1
            audit.append(dict(source_id=r.source_id, channel=c, rank=rank, reason=reason))
    return dict(method=VERSION, selected=chosen, audit=audit)
