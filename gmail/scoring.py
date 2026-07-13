"""Pure scoring functions for intelligent email cleanup.

No I/O, no globals except the config module. All inputs are passed explicitly so
tests and the runtime read the same code path.

Two orthogonal axes per email:
- protect_score : likelihood the email is important (should never be deleted)
- junk_score    : likelihood the email is disposable
delete_confidence = max(0, junk_score - protect_score)

A preset then decides whether an email is recommended for deletion based on
delete_confidence and whether any single protect signal is strong enough to
veto the recommendation (see config.CLEANUP_PRESETS).
"""

from typing import Iterable, Mapping, Optional

import config


def is_transactional(subject_lower: str, sender_domain: str) -> bool:
    """True if the subject or sender domain looks like a receipt/invoice/order."""
    if subject_lower and config.TRANSACTIONAL_SUBJECT_REGEX.search(subject_lower):
        return True
    if not sender_domain:
        return False
    domain = sender_domain.lower()
    if domain in config.TRANSACTIONAL_DOMAIN_SEEDS:
        return True
    # Sub-domain match: newsletter.chase.com -> chase.com
    for seed in config.TRANSACTIONAL_DOMAIN_SEEDS:
        if domain.endswith('.' + seed):
            return True
    return False


def _matches_allowlist(sender_email: str, sender_domain: str,
                       allowlist: Iterable[dict]) -> bool:
    """Check whether the sender is covered by any allowlist entry.

    Entries are dicts of shape {'type': 'sender_email'|'sender_domain', 'value': ...}.
    """
    for entry in allowlist or ():
        value = (entry.get('value') or '').lower()
        if not value:
            continue
        etype = entry.get('type')
        if etype == 'sender_email' and sender_email == value:
            return True
        if etype == 'sender_domain':
            if sender_domain == value or sender_domain.endswith('.' + value):
                return True
    return False


def _read_rate_tier(stats: Optional[Mapping]) -> Optional[str]:
    """Classify a sender's engagement based on read rate. Returns 'high',
    'medium', 'zero', or None (insufficient data)."""
    if not stats:
        return None
    total = stats.get('total', 0)
    if total < config.READ_RATE_MIN_SAMPLES:
        return None
    read_rate = stats.get('read_rate', 0.0)
    if read_rate >= config.READ_RATE_HIGH:
        return 'high'
    if read_rate >= config.READ_RATE_MEDIUM_MIN:
        return 'medium'
    if read_rate == 0.0 and total >= config.ZERO_READ_MIN_SAMPLES:
        return 'zero'
    return None


def score_email(email: Mapping,
                sender_stats: Optional[Mapping] = None,
                engaged_senders: Optional[Iterable[str]] = None,
                allowlist: Optional[Iterable[dict]] = None,
                denylist: Optional[Iterable[dict]] = None) -> dict:
    """Compute protect/junk scores for a single parsed email dict.

    email must contain at minimum:
      sender_email, sender_domain, subject_lower, labels, has_unsubscribe,
      is_unread, age_days, is_starred, is_important

    sender_stats is the per-sender aggregate for this email's sender (or None).
    engaged_senders is a set/collection of sender emails you've written to.
    allowlist / denylist are lists of {type, value} dicts.
    """
    sender_email = (email.get('sender_email') or '').lower()
    sender_domain = (email.get('sender_domain') or '').lower()
    subject_lower = (email.get('subject_lower') or '').lower()
    labels = email.get('labels') or []

    engaged_set = set(engaged_senders or ())

    protect_score = 0
    protect_reasons = []
    junk_score = 0
    junk_reasons = []

    # ----- Protect signals -----
    if _matches_allowlist(sender_email, sender_domain, allowlist or ()):
        protect_score += config.PROTECT_WEIGHTS['allowlisted']
        protect_reasons.append('allowlisted')

    if sender_email and sender_email in engaged_set:
        protect_score += config.PROTECT_WEIGHTS['you_replied']
        protect_reasons.append('you_replied')

    tier = _read_rate_tier(sender_stats)
    if tier == 'high':
        protect_score += config.PROTECT_WEIGHTS['high_read_rate']
        protect_reasons.append('high_read_rate')
    elif tier == 'medium':
        protect_score += config.PROTECT_WEIGHTS['medium_read_rate']
        protect_reasons.append('medium_read_rate')

    if email.get('is_starred'):
        protect_score += config.PROTECT_WEIGHTS['starred']
        protect_reasons.append('starred')

    if is_transactional(subject_lower, sender_domain):
        protect_score += config.PROTECT_WEIGHTS['transactional']
        protect_reasons.append('transactional')

    if email.get('is_important'):
        protect_score += config.PROTECT_WEIGHTS['important_label']
        protect_reasons.append('important_label')

    # ----- Junk signals -----
    if _matches_allowlist(sender_email, sender_domain, denylist or ()):
        # reuse the same matcher — denylist has the same shape as allowlist
        junk_score += config.JUNK_WEIGHTS['denylisted']
        junk_reasons.append('denylisted')

    if tier == 'zero':
        junk_score += config.JUNK_WEIGHTS['zero_read_rate']
        junk_reasons.append('zero_read_rate')

    if email.get('is_unread') and (email.get('age_days') or 0) > config.UNREAD_OLD_AGE_DAYS:
        junk_score += config.JUNK_WEIGHTS['unread_and_old']
        junk_reasons.append('unread_and_old')

    if email.get('has_unsubscribe'):
        junk_score += config.JUNK_WEIGHTS['has_unsubscribe']
        junk_reasons.append('has_unsubscribe')

    if 'CATEGORY_PROMOTIONS' in labels:
        junk_score += config.JUNK_WEIGHTS['promotions_category']
        junk_reasons.append('promotions_category')

    delete_confidence = max(0, junk_score - protect_score)

    return {
        'protect_score': protect_score,
        'protect_reasons': protect_reasons,
        'junk_score': junk_score,
        'junk_reasons': junk_reasons,
        'delete_confidence': delete_confidence,
    }


def apply_preset(scored: Mapping, preset_name: str) -> dict:
    """Decide whether a scored email is recommended for deletion under the preset."""
    preset = config.CLEANUP_PRESETS.get(preset_name)
    if not preset:
        preset = config.CLEANUP_PRESETS[config.DEFAULT_PRESET]

    protect_score = scored.get('protect_score', 0)
    confidence = scored.get('delete_confidence', 0)

    # Compute strongest single protect signal (weight-based veto).
    strongest_protect = max(
        (config.PROTECT_WEIGHTS.get(r, 0) for r in scored.get('protect_reasons', ())),
        default=0,
    )

    recommend = (
        confidence >= preset['min_confidence']
        and strongest_protect < preset['max_protect_score']
    )

    return {
        'recommend_delete': bool(recommend),
        'confidence': confidence,
        'strongest_protect': strongest_protect,
        'preset': preset_name,
    }


def score_and_decide(email: Mapping,
                     preset_name: str,
                     sender_stats: Optional[Mapping] = None,
                     engaged_senders: Optional[Iterable[str]] = None,
                     allowlist: Optional[Iterable[dict]] = None,
                     denylist: Optional[Iterable[dict]] = None) -> dict:
    """Convenience: score + preset decision in one call."""
    scored = score_email(email, sender_stats, engaged_senders, allowlist, denylist)
    decision = apply_preset(scored, preset_name)
    return {**scored, **decision}
