"""Independent stage summaries for all four recommendation sources."""
from app.services.feed import curation, discovery, domain_progress, profile as profile_service
from app.services.literature_harvester import literature_index_harvester


def phase(*, configured, active, failed, pending, translating, ready, completed, identity_pending=0):
    if not configured:
        return 'not_configured'
    if active:
        return 'searching'
    if pending:
        return 'preparing'
    if translating:
        return 'translating'
    if failed:
        return 'failed'
    if identity_pending and not ready:
        return 'identity_pending'
    if ready:
        return 'ready'
    return 'empty' if completed else 'waiting'


def attach(progress, user, profile):
    if not progress.source_progress:
        return
    uid = str(user.id)
    state = curation.stored(user)
    subscriptions = state.get('subscriptions') or {}
    history = state.get('subscription_discovery') or {}
    domain_states = domain_progress.read(uid)
    domain_codes = profile_service.stored_domains(user)
    domain_values = [domain_states.get(code, {}) for code in domain_codes]
    active_kind = discovery._subscription_active_targets.get(uid)
    journal_task = literature_index_harvester._online_tasks.get('subscription:' + uid)
    selected = {
        'domain': bool(domain_codes), 'journal': bool(profile.journals),
        'scholar': bool(subscriptions.get('scholars')), 'project': bool(profile.projects),
    }
    active = {
        'domain': literature_index_harvester.is_online_refreshing(uid),
        'journal': active_kind == 'journals' or bool(journal_task and not journal_task.done()),
        'scholar': active_kind == 'scholars',
        'project': bool(discovery._project_inflight.get(uid)),
    }
    for row in progress.source_progress:
        source = row['source']
        row['recommendable'] = sum(group['total'] for group in progress.source_groups if source in group['sources'])
        row['ready'] = sum(group['translated'] for group in progress.source_groups if source in group['sources'])
        row['translation_pending'] = row['recommendable'] - row['ready']
        failed, completed, unresolved = False, False, 0
        if source == 'domain':
            failed = any(value.get('state') in {'failed', 'searching'} for value in domain_values) and not active[source]
            completed = bool(domain_values) and all(value.get('state') == 'completed' for value in domain_values)
        elif source in {'journal', 'scholar'}:
            kind = source + 's'
            entries = subscriptions.get(kind) or []
            values = [history.get(discovery._subscription_key(kind, entry), {}) for entry in entries]
            failed = any(value.get('error') for value in values)
            completed = bool(values) and all(value.get('succeeded_at') for value in values)
            if source == 'scholar':
                from app.services.feed.author_identity import identity
                unresolved = sum(not identity(entry) for entry in entries)
        else:
            failed = bool(state.get('curated_error'))
            completed = bool(state.get('curated_at') and profile.active_project_queries)
        row['configured'] = selected[source]
        row['identity_pending'] = unresolved
        row['phase'] = phase(configured=selected[source], active=active[source], failed=failed,
            pending=row['pending'], translating=row['translation_pending'], ready=row['ready'],
            completed=completed, identity_pending=unresolved)
