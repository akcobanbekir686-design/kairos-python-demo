"""Experimental A ranking adapter: trusted vectors in, auditable pool out.
No text embedding, model calls, persistence, or production authority.
"""
from dataclasses import dataclass
import math
from calculation import Record, eligible, select

METHOD = 'kairos-a-pairwise-cosine-draft-0.1'

@dataclass(frozen=True)
class Embedding:
    record: Record
    vector: tuple | None
    model_revision: str
    preprocessing_revision: str


def unit(vector):
    if vector is None:
        return None, 'missing_vector'
    if not isinstance(vector, tuple) or not vector:
        return None, 'invalid_vector'
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        return None, 'invalid_vector'
    scale = max(abs(x) for x in vector)
    if scale == 0:
        return None, 'zero_vector'
    scaled = [x / scale for x in vector]
    norm = math.sqrt(math.fsum(x*x for x in scaled))
    return tuple(x / norm for x in scaled), None


def rank_history(current, history, *, owner, current_session):
    """No threshold or inferred timestamp. Ties use source ID, never input order.
    A partial pool is inspectable but blocked by select_history below.
    All eligible examined source IDs must become dependencies when persisted.
    """
    if not isinstance(current, Embedding):
        raise ValueError('embedding_required')
    r = current.record
    if (not isinstance(r, Record) or not r.source_id or r.owner != owner
            or r.session_id != current_session or r.origin != 'user_narrative'
            or r.status != 'active' or r.entertainment or r.session_state != 'open'):
        raise ValueError('current_user_narrative_required')
    if not current.model_revision.strip() or not current.preprocessing_revision.strip():
        raise ValueError('explicit_revisions_required')
    base = dict(method=METHOD, model_revision=current.model_revision,
                preprocessing_revision=current.preprocessing_revision,
                current_source_id=r.source_id, ranked=[], pool=[], audit=[],
                dependency_source_ids=[r.source_id], threshold=None,
                tie_break='source_id_ascending')
    cv, reason = unit(current.vector)
    if reason:
        return dict(base, state='unavailable', reason=reason)
    snapshots = {}
    for item in history:
        if not isinstance(item, Embedding) or not isinstance(item.record, Record) or not item.record.source_id:
            raise ValueError('embedding_record_required')
        sid = item.record.source_id
        if sid == r.source_id:
            raise ValueError('current_source_in_history')
        if sid in snapshots and snapshots[sid] != item:
            raise ValueError('conflicting_source_snapshot')
        snapshots[sid] = item
    scored, incomplete = [], False
    for sid, item in sorted(snapshots.items()):
        if not eligible(item.record, owner, current_session):
            base['audit'].append(dict(source_id=sid, reason='ineligible'))
            continue
        base['dependency_source_ids'].append(sid)
        vec, reason = unit(item.vector)
        if (item.model_revision != current.model_revision or
                item.preprocessing_revision != current.preprocessing_revision):
            reason = 'revision_mismatch'
        elif not reason and len(vec) != len(cv):
            reason = 'dimension_mismatch'
        if reason:
            incomplete = True
            base['audit'].append(dict(source_id=sid, reason=reason))
            continue
        cosine = max(-1.0, min(1.0, math.fsum(a*b for a,b in zip(cv,vec))))
        scored.append((cosine, sid, item.record))
    scored.sort(key=lambda v: (-v[0], v[1]))
    for rank, (cosine, sid, record) in enumerate(scored, 1):
        base['ranked'].append(dict(source_id=sid, cosine=cosine,
                                   normalized_similarity=(cosine+1)/2, rank=rank))
        base['audit'].append(dict(source_id=sid, reason='in_pool' if rank<=12 else 'pool_cap', rank=rank))
        if rank <= 12:
            base['pool'].append(record)
    state = 'history_incomplete' if incomplete else ('ranked' if scored else 'no_history')
    return dict(base, state=state)


def select_history(current, history, *, symbol_pool=(), recent_pool=(), owner, current_session):
    """Feed complete A ranking to existing 3+1+1 selector; other pools trusted.
    Does not give missing A budget to other channels or infer their ordering.
    """
    ranking = rank_history(current, history, owner=owner, current_session=current_session)
    if ranking['state'] not in ('ranked', 'no_history'):
        return dict(state='blocked', ranking=ranking, selection=None)
    result = select({'A': ranking['pool'], 'symbol': symbol_pool, 'recent': recent_pool},
                    owner=owner, current_session=current_session)
    return dict(state='selected', ranking=ranking, selection=result)
