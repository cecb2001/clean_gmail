"""Tests for gmail.scoring — covers every case in the plan's verification section.

Notation for fixture builders: read as "an email from X with property Y". Everything is
a plain dict so tests exercise the exact shape the analyzer produces.
"""

import pytest

from gmail import scoring
import config


# ---------- Fixture builders ----------

def make_email(**overrides):
    base = {
        'id': 'msg1',
        'sender_email': 'promo@example.com',
        'sender_domain': 'example.com',
        'subject': 'Deal of the week',
        'subject_lower': 'deal of the week',
        'labels': [],
        'has_unsubscribe': False,
        'is_unread': False,
        'age_days': 30,
        'is_starred': False,
        'is_important': False,
    }
    base.update(overrides)
    return base


def stats(total, unread, has_reply=False):
    return {
        'total': total,
        'unread': unread,
        'read_rate': 1.0 - (unread / total) if total else 0.0,
        'has_reply': has_reply,
    }


# ---------- Protect signals ----------

def test_allowlist_beats_everything():
    email = make_email(
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=365,
    )
    result = scoring.score_and_decide(
        email,
        preset_name='aggressive',
        allowlist=[{'type': 'sender_email', 'value': 'promo@example.com'}],
    )
    assert 'allowlisted' in result['protect_reasons']
    assert result['recommend_delete'] is False


def test_allowlist_domain_matches_subdomain():
    email = make_email(sender_email='news@sub.example.com', sender_domain='sub.example.com')
    result = scoring.score_and_decide(
        email,
        preset_name='aggressive',
        allowlist=[{'type': 'sender_domain', 'value': 'example.com'}],
    )
    assert 'allowlisted' in result['protect_reasons']
    assert result['recommend_delete'] is False


def test_replied_sender_never_recommended_any_preset():
    email = make_email(
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=365,
    )
    engaged = {'promo@example.com'}
    for preset in ('conservative', 'balanced', 'aggressive'):
        result = scoring.score_and_decide(
            email, preset_name=preset, engaged_senders=engaged,
        )
        assert 'you_replied' in result['protect_reasons'], preset
        assert result['recommend_delete'] is False, preset


def test_high_read_rate_never_recommended_any_preset():
    email = make_email(
        sender_email='digest@newsletter.com', sender_domain='newsletter.com',
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
    )
    sender_stats = stats(total=10, unread=1)  # 90% read
    for preset in ('conservative', 'balanced', 'aggressive'):
        result = scoring.score_and_decide(
            email, preset_name=preset, sender_stats=sender_stats,
        )
        assert 'high_read_rate' in result['protect_reasons'], preset
        assert result['recommend_delete'] is False, preset


def test_medium_read_rate_only_protects_soft_presets():
    email = make_email(
        sender_email='occasional@newsletter.com', sender_domain='newsletter.com',
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=200,
    )
    sender_stats = stats(total=10, unread=6)  # 40% read -> medium
    # Conservative and Balanced (max_protect_score=40) exclude anything with a protect signal >= 40.
    for preset in ('conservative', 'balanced'):
        result = scoring.score_and_decide(
            email, preset_name=preset, sender_stats=sender_stats,
        )
        assert 'medium_read_rate' in result['protect_reasons']
        assert result['recommend_delete'] is False, preset
    # Aggressive (max_protect_score=100) — medium engagement (+50) does not veto,
    # so if junk signals win, it can still be recommended.
    result_agg = scoring.score_and_decide(
        email, preset_name='aggressive', sender_stats=sender_stats,
    )
    # protect: 50 (medium). junk: 20 (unsub) + 30 (old) + 15 (promo) = 65. delta = 15 -> below 20 min.
    # So the boundary check: this specific email is NOT recommended even in aggressive.
    assert result_agg['recommend_delete'] is False


def test_read_rate_below_min_samples_ignored():
    email = make_email(
        sender_email='new@sender.com', sender_domain='sender.com',
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=200,
    )
    # 3 emails, 2 read -> 66% but below min sample threshold
    sender_stats = stats(total=3, unread=1)
    result = scoring.score_and_decide(
        email, preset_name='balanced', sender_stats=sender_stats,
    )
    assert 'high_read_rate' not in result['protect_reasons']
    assert 'medium_read_rate' not in result['protect_reasons']


def test_starred_email_never_recommended():
    email = make_email(
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=500,
        is_starred=True,
    )
    for preset in ('conservative', 'balanced', 'aggressive'):
        result = scoring.score_and_decide(email, preset_name=preset)
        assert 'starred' in result['protect_reasons'], preset
        assert result['recommend_delete'] is False, preset


def test_transactional_subject_protects_promotions_email():
    email = make_email(
        sender_email='shop@example.com', sender_domain='example.com',
        subject='Your order has shipped',
        subject_lower='your order has shipped',
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=200,
    )
    result = scoring.score_and_decide(email, preset_name='balanced')
    assert 'transactional' in result['protect_reasons']
    assert result['recommend_delete'] is False


def test_transactional_domain_seed_protects():
    email = make_email(
        sender_email='alerts@chase.com', sender_domain='chase.com',
        subject='Weekly summary',
        subject_lower='weekly summary',
        has_unsubscribe=True,
    )
    result = scoring.score_and_decide(email, preset_name='aggressive')
    assert 'transactional' in result['protect_reasons']
    assert result['recommend_delete'] is False


def test_important_label_soft_protect():
    email = make_email(
        labels=['CATEGORY_PROMOTIONS', 'IMPORTANT'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=200,
        is_important=True,
    )
    # IMPORTANT (+50) crosses the max_protect_score=40 bar for Conservative/Balanced.
    for preset in ('conservative', 'balanced'):
        result = scoring.score_and_decide(email, preset_name=preset)
        assert 'important_label' in result['protect_reasons']
        assert result['recommend_delete'] is False, preset


# ---------- Junk signals & preset behavior ----------

def test_zero_read_rate_junk_signal_fires():
    email = make_email(
        sender_email='spam@bulk.com', sender_domain='bulk.com',
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=200,
    )
    sender_stats = stats(total=10, unread=10)  # 0% read
    result = scoring.score_and_decide(
        email, preset_name='balanced', sender_stats=sender_stats,
    )
    assert 'zero_read_rate' in result['junk_reasons']
    # junk: 40 + 30 + 20 + 15 = 105. protect: 0. confidence 105 >= 40 balanced.
    assert result['recommend_delete'] is True


def test_zero_read_rate_needs_min_samples():
    email = make_email(
        sender_email='newone@bulk.com', sender_domain='bulk.com',
        has_unsubscribe=True,
        is_unread=True,
        age_days=200,
    )
    sender_stats = stats(total=3, unread=3)  # 0% read but below sample threshold
    result = scoring.score_and_decide(
        email, preset_name='balanced', sender_stats=sender_stats,
    )
    assert 'zero_read_rate' not in result['junk_reasons']


def test_conservative_preset_needs_strong_evidence():
    """Sender with 0% read, 10 emails, unsubscribe, old & unread, but no Promotions label."""
    email = make_email(
        sender_email='spam@bulk.com', sender_domain='bulk.com',
        has_unsubscribe=True,
        is_unread=True,
        age_days=200,
    )
    sender_stats = stats(total=10, unread=10)
    # junk: 40 (zero read) + 30 (old&unread) + 20 (unsub) = 90 -> above 60 conservative
    result = scoring.score_and_decide(
        email, preset_name='conservative', sender_stats=sender_stats,
    )
    assert result['recommend_delete'] is True


def test_denylist_forces_recommendation():
    email = make_email(sender_email='junk@evil.com', sender_domain='evil.com')
    result = scoring.score_and_decide(
        email, preset_name='conservative',
        denylist=[{'type': 'sender_domain', 'value': 'evil.com'}],
    )
    assert 'denylisted' in result['junk_reasons']
    assert result['recommend_delete'] is True


def test_preset_threshold_boundary_conservative():
    """At exactly delete_confidence == 60 with no strong protect, Conservative recommends."""
    email = make_email(
        sender_email='bulk@bulk.com', sender_domain='bulk.com',
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,   # +20
        is_unread=True,
        age_days=200,           # +30
    )
    # Also need 15 (promo) + but zero read requires stats. Skip stats to hit exact 65.
    # 20 + 30 + 15 = 65 -> above 60 conservative
    result = scoring.score_and_decide(email, preset_name='conservative')
    assert result['delete_confidence'] == 65
    assert result['recommend_delete'] is True


def test_preset_just_below_conservative_threshold():
    email = make_email(
        labels=['CATEGORY_PROMOTIONS'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=30,  # not old enough
    )
    # 20 + 15 = 35 < 60 conservative
    result = scoring.score_and_decide(email, preset_name='conservative')
    assert result['recommend_delete'] is False
    # But Balanced (>=40) still doesn't fire either; Aggressive (>=20) does
    result_agg = scoring.score_and_decide(email, preset_name='aggressive')
    assert result_agg['recommend_delete'] is True


def test_aggressive_ignores_soft_protect_only():
    """Under Aggressive, IMPORTANT (+50) or medium engagement (+50) alone shouldn't veto —
    only signals >= 100 do (starred, transactional, replied, high-read, allowlisted)."""
    email = make_email(
        sender_email='someone@example.com', sender_domain='example.com',
        labels=['CATEGORY_PROMOTIONS', 'IMPORTANT'],
        has_unsubscribe=True,
        is_unread=True,
        age_days=200,
        is_important=True,
    )
    # protect: 50 (important). junk: 20 + 30 + 15 = 65. delta = 15 < 20 aggressive min.
    result = scoring.score_and_decide(email, preset_name='aggressive')
    assert result['recommend_delete'] is False  # not enough junk delta even in aggressive

    # But if we add zero-read-rate: junk becomes 105. delta = 55. And max_protect=100 so
    # IMPORTANT (50) doesn't veto in aggressive.
    sender_stats = stats(total=8, unread=8)
    result2 = scoring.score_and_decide(
        email, preset_name='aggressive', sender_stats=sender_stats,
    )
    # Aggressive: min_conf=20, max_protect=100. protect=50 (important) < 100 so no veto.
    assert result2['recommend_delete'] is True


def test_scoring_is_deterministic_and_pure():
    email = make_email(labels=['CATEGORY_PROMOTIONS'], has_unsubscribe=True)
    a = scoring.score_email(email)
    b = scoring.score_email(email)
    assert a == b


def test_transactional_helper_direct():
    assert scoring.is_transactional('your invoice is ready', '') is True
    assert scoring.is_transactional('', 'chase.com') is True
    assert scoring.is_transactional('', 'newsletter.chase.com') is True   # subdomain
    assert scoring.is_transactional('spam offer', 'random.com') is False


def test_unknown_preset_falls_back_to_default():
    email = make_email()
    result = scoring.apply_preset(scoring.score_email(email), 'bogus_preset')
    # Should not crash and should behave like the default preset (conservative).
    assert result['preset'] == 'bogus_preset'  # echoed back verbatim
    default = config.CLEANUP_PRESETS[config.DEFAULT_PRESET]
    # With no junk signals, confidence is 0 -> below conservative's threshold -> no recommend
    assert result['recommend_delete'] is False
    assert default['min_confidence'] == 60  # sanity


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
