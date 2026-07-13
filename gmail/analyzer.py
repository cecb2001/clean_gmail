import re
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

import config
from gmail import scoring


class EmailAnalyzer:
    """Analyzes emails and detects patterns for cleanup suggestions."""

    def __init__(self, gmail_client, engaged_senders=None, allowlist=None,
                 denylist=None, preset=None):
        self.client = gmail_client
        self._emails = []
        self._analysis_cache = None
        # Intelligence inputs. Callers can update these before running analyze().
        self.engaged_senders = set(engaged_senders or ())
        self.allowlist = list(allowlist or [])
        self.denylist = list(denylist or [])
        self.preset = preset or config.DEFAULT_PRESET
        # Per-sender aggregates. Populated in _compute_sender_stats().
        self._sender_stats = {}

    def fetch_and_analyze(self, query='', label_ids=None, max_emails=None, progress_callback=None):
        max_emails = max_emails or config.MAX_EMAILS_TO_ANALYZE
        self._emails = []
        self._analysis_cache = None

        if progress_callback:
            progress_callback(0, 0, 'fetching_ids')

        message_ids = []
        for msg in self.client.fetch_all_messages(
            query=query,
            label_ids=label_ids,
            max_total=max_emails
        ):
            message_ids.append(msg['id'])

        if not message_ids:
            return self._empty_analysis()

        if progress_callback:
            progress_callback(0, len(message_ids), 'fetching_metadata')

        metadata_headers = ['From', 'To', 'Subject', 'Date', 'List-Unsubscribe']
        batch_size = 100

        for i in range(0, len(message_ids), batch_size):
            batch_ids = message_ids[i:i + batch_size]
            messages = self.client.get_messages_batch(
                batch_ids,
                format='metadata',
                metadata_headers=metadata_headers
            )

            for msg in messages:
                if 'error' not in msg:
                    self._emails.append(self._parse_message(msg))

            if progress_callback:
                progress_callback(
                    min(i + batch_size, len(message_ids)),
                    len(message_ids),
                    'fetching_metadata'
                )

        if progress_callback:
            progress_callback(0, 0, 'analyzing')

        return self.analyze()

    def _parse_message(self, msg):
        headers = {}
        for header in msg.get('payload', {}).get('headers', []):
            headers[header['name'].lower()] = header['value']

        from_header = headers.get('from', '')
        sender_name, sender_email = parseaddr(from_header)
        sender_email = (sender_email or '').lower()
        sender_domain = sender_email.split('@')[-1] if '@' in sender_email else ''

        date_str = headers.get('date', '')
        try:
            date = parsedate_to_datetime(date_str)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except Exception:
            date = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        age_days = (now - date).days

        labels = msg.get('labelIds', []) or []
        subject = headers.get('subject', '')

        return {
            'id': msg['id'],
            'thread_id': msg.get('threadId'),
            'labels': labels,
            'snippet': msg.get('snippet', ''),
            'size_bytes': msg.get('sizeEstimate', 0),
            'sender_email': sender_email,
            'sender_name': sender_name,
            'sender_domain': sender_domain.lower(),
            'subject': subject,
            'subject_lower': subject.lower(),
            'date': date,
            'age_days': age_days,
            'has_unsubscribe': 'list-unsubscribe' in headers,
            'is_unread': 'UNREAD' in labels,
            'is_starred': 'STARRED' in labels,
            'is_important': 'IMPORTANT' in labels,
        }

    # ----- intelligence -----

    def set_intelligence_inputs(self, engaged_senders=None, allowlist=None,
                                denylist=None, preset=None):
        """Update the intelligence inputs. Clears the cached analysis so a
        subsequent analyze() re-runs scoring against the same fetched emails."""
        if engaged_senders is not None:
            self.engaged_senders = set(engaged_senders)
        if allowlist is not None:
            self.allowlist = list(allowlist)
        if denylist is not None:
            self.denylist = list(denylist)
        if preset is not None:
            self.preset = preset
        self._analysis_cache = None

    def _compute_sender_stats(self):
        """Per-sender aggregates used by the scoring engine."""
        stats = defaultdict(lambda: {'total': 0, 'unread': 0, 'starred': 0})
        for e in self._emails:
            s = e['sender_email']
            if not s:
                continue
            entry = stats[s]
            entry['total'] += 1
            if e['is_unread']:
                entry['unread'] += 1
            if e['is_starred']:
                entry['starred'] += 1

        # Derive read_rate and reply flag.
        engaged = self.engaged_senders
        for s, entry in stats.items():
            total = entry['total']
            entry['read_rate'] = (1.0 - entry['unread'] / total) if total else 0.0
            entry['has_reply'] = s in engaged
        self._sender_stats = dict(stats)

    def _score_all_emails(self):
        """Attach a scored/decision dict to each email in place."""
        for e in self._emails:
            e['scored'] = scoring.score_and_decide(
                e,
                preset_name=self.preset,
                sender_stats=self._sender_stats.get(e['sender_email']),
                engaged_senders=self.engaged_senders,
                allowlist=self.allowlist,
                denylist=self.denylist,
            )

    # ----- analysis entry points -----

    def analyze(self):
        if self._analysis_cache:
            return self._analysis_cache

        if not self._emails:
            return self._empty_analysis()

        self._compute_sender_stats()
        self._score_all_emails()

        by_sender_domain = self._group_by_sender_domain()
        by_sender_email = self._group_by_sender_email()
        by_category = self._group_by_category()
        by_age = self._group_by_age()
        by_size = self._group_by_size()
        newsletters = self._find_newsletters()

        # Annotate every pattern in every group with protected_count + avg_confidence.
        for group in (by_sender_domain, by_sender_email, by_category, by_age, by_size, newsletters):
            for pattern in group:
                self._annotate_pattern(pattern)

        by_recommended_cleanup = self._compute_recommendations(
            candidate_groups=(by_sender_email, by_sender_domain, newsletters),
        )

        analysis = {
            'total_emails': len(self._emails),
            'total_size_bytes': sum(e['size_bytes'] for e in self._emails),
            'preset': self.preset,
            'presets_available': [
                {'key': k, 'label': v['label'], 'description': v['description']}
                for k, v in config.CLEANUP_PRESETS.items()
            ],
            'patterns': {
                'by_recommended_cleanup': by_recommended_cleanup,
                'by_sender_domain': by_sender_domain,
                'by_sender_email': by_sender_email,
                'by_category': by_category,
                'by_age': by_age,
                'by_size': by_size,
                'newsletters': newsletters,
            },
            'summary': self._generate_summary(),
        }

        self._analysis_cache = analysis
        return analysis

    def rescore(self):
        """Re-run scoring with the current preset/allowlist/denylist against the
        already-fetched emails. Skips the fetch entirely."""
        self._analysis_cache = None
        return self.analyze()

    def _annotate_pattern(self, pattern):
        """Add protected_count, unprotected_count, avg_confidence to a pattern row.

        Looks up each pattern's email_ids against self._emails' scored data. This
        is O(n) but only runs at analysis time; the pattern list is small.
        """
        ids = set(pattern.get('email_ids') or [])
        if not ids:
            pattern['protected_count'] = 0
            pattern['unprotected_count'] = 0
            pattern['avg_confidence'] = 0
            return

        protected = 0
        unprotected_ids = []
        conf_sum = 0
        for e in self._emails:
            if e['id'] not in ids:
                continue
            scored = e.get('scored', {})
            if scored.get('recommend_delete'):
                unprotected_ids.append(e['id'])
                conf_sum += scored.get('confidence', 0)
            else:
                protected += 1

        pattern['protected_count'] = protected
        pattern['unprotected_count'] = len(unprotected_ids)
        pattern['unprotected_email_ids'] = unprotected_ids
        pattern['avg_confidence'] = int(conf_sum / len(unprotected_ids)) if unprotected_ids else 0

    def _compute_recommendations(self, candidate_groups):
        """Rank pattern rows by (unprotected_count * avg_confidence) to surface
        the biggest safe cleanup opportunities first.

        Only patterns with >= MIN_PATTERN_COUNT unprotected emails are kept.
        Reuses the annotations already computed by _annotate_pattern.
        """
        # Dedup patterns across groups by (type, key). sender_email > sender_domain > newsletter.
        seen = set()
        candidates = []
        for group in candidate_groups:
            for pattern in group:
                key = (pattern['type'], pattern['key'])
                if key in seen:
                    continue
                seen.add(key)
                if pattern.get('unprotected_count', 0) < config.MIN_PATTERN_COUNT:
                    continue
                # Wrap so we don't mutate the source patterns and their scores.
                candidates.append({
                    # Distinct type so the pattern-lookup endpoints don't
                    # confuse a recommendation entry (unprotected-only ids)
                    # with the source pattern (all ids, all fields).
                    'type': 'recommended_' + pattern['type'],
                    'key': pattern['key'],
                    'display': pattern['display'],
                    'unprotected_count': pattern['unprotected_count'],
                    'protected_count': pattern.get('protected_count', 0),
                    'avg_confidence': pattern.get('avg_confidence', 0),
                    'email_ids': pattern.get('unprotected_email_ids', []),
                    'source_total': pattern.get('count', 0),
                    # Alias so /api/pattern/... endpoints that read `count`
                    # keep working without a special case.
                    'count': pattern['unprotected_count'],
                    'size_bytes': pattern.get('size_bytes', 0),
                    'unread': pattern.get('unread', 0),
                })

        candidates.sort(
            key=lambda p: p['unprotected_count'] * max(p['avg_confidence'], 1),
            reverse=True,
        )
        return candidates

    # ----- existing pattern groupings (unchanged semantics) -----

    def _group_by_sender_domain(self):
        groups = defaultdict(lambda: {'count': 0, 'size_bytes': 0, 'emails': [], 'unread': 0})
        for email in self._emails:
            domain = email['sender_domain']
            if domain:
                groups[domain]['count'] += 1
                groups[domain]['size_bytes'] += email['size_bytes']
                groups[domain]['emails'].append(email['id'])
                if email['is_unread']:
                    groups[domain]['unread'] += 1

        result = []
        for domain, data in groups.items():
            if data['count'] >= config.MIN_PATTERN_COUNT:
                result.append({
                    'type': 'sender_domain',
                    'key': domain,
                    'display': domain,
                    'count': data['count'],
                    'size_bytes': data['size_bytes'],
                    'unread': data['unread'],
                    'email_ids': data['emails'],
                })
        return sorted(result, key=lambda x: x['size_bytes'], reverse=True)

    def _group_by_sender_email(self):
        groups = defaultdict(lambda: {'count': 0, 'size_bytes': 0, 'emails': [], 'unread': 0, 'name': ''})
        for email in self._emails:
            sender = email['sender_email']
            if sender:
                groups[sender]['count'] += 1
                groups[sender]['size_bytes'] += email['size_bytes']
                groups[sender]['emails'].append(email['id'])
                if email['is_unread']:
                    groups[sender]['unread'] += 1
                if not groups[sender]['name'] and email['sender_name']:
                    groups[sender]['name'] = email['sender_name']

        result = []
        for sender, data in groups.items():
            if data['count'] >= config.MIN_PATTERN_COUNT:
                result.append({
                    'type': 'sender_email',
                    'key': sender,
                    'display': f"{data['name']} <{sender}>" if data['name'] else sender,
                    'count': data['count'],
                    'size_bytes': data['size_bytes'],
                    'unread': data['unread'],
                    'email_ids': data['emails'],
                })
        return sorted(result, key=lambda x: x['size_bytes'], reverse=True)

    def _group_by_category(self):
        groups = defaultdict(lambda: {'count': 0, 'size_bytes': 0, 'emails': [], 'unread': 0})
        category_display = {
            'CATEGORY_PROMOTIONS': 'Promotions',
            'CATEGORY_SOCIAL': 'Social',
            'CATEGORY_UPDATES': 'Updates',
            'CATEGORY_FORUMS': 'Forums',
            'CATEGORY_PERSONAL': 'Primary',
        }
        for email in self._emails:
            for label in email['labels']:
                if label.startswith('CATEGORY_'):
                    groups[label]['count'] += 1
                    groups[label]['size_bytes'] += email['size_bytes']
                    groups[label]['emails'].append(email['id'])
                    if email['is_unread']:
                        groups[label]['unread'] += 1
        result = []
        for category, data in groups.items():
            result.append({
                'type': 'category',
                'key': category,
                'display': category_display.get(category, category),
                'count': data['count'],
                'size_bytes': data['size_bytes'],
                'unread': data['unread'],
                'email_ids': data['emails'],
            })
        return sorted(result, key=lambda x: x['size_bytes'], reverse=True)

    def _group_by_age(self):
        age_groups = config.AGE_GROUPS_DAYS
        groups = {days: {'count': 0, 'size_bytes': 0, 'emails': [], 'unread': 0}
                  for days in age_groups}
        for email in self._emails:
            for days in age_groups:
                if email['age_days'] >= days:
                    groups[days]['count'] += 1
                    groups[days]['size_bytes'] += email['size_bytes']
                    groups[days]['emails'].append(email['id'])
                    if email['is_unread']:
                        groups[days]['unread'] += 1
                    break

        result = []
        labels = {
            30: 'Older than 1 month',
            90: 'Older than 3 months',
            180: 'Older than 6 months',
            365: 'Older than 1 year',
            730: 'Older than 2 years',
        }
        for days in age_groups:
            data = groups[days]
            if data['count'] >= config.MIN_PATTERN_COUNT:
                result.append({
                    'type': 'age',
                    'key': f'older_than_{days}_days',
                    'display': labels.get(days, f'Older than {days} days'),
                    'count': data['count'],
                    'size_bytes': data['size_bytes'],
                    'unread': data['unread'],
                    'email_ids': data['emails'],
                    'age_days': days,
                })
        return result

    def _group_by_size(self):
        threshold = config.LARGE_EMAIL_SIZE_KB * 1024
        large_emails = [e for e in self._emails if e['size_bytes'] >= threshold]
        if len(large_emails) < config.MIN_PATTERN_COUNT:
            return []
        large_emails.sort(key=lambda x: x['size_bytes'], reverse=True)
        return [{
            'type': 'size',
            'key': 'large_emails',
            'display': f'Large emails (>{config.LARGE_EMAIL_SIZE_KB}KB)',
            'count': len(large_emails),
            'size_bytes': sum(e['size_bytes'] for e in large_emails),
            'unread': sum(1 for e in large_emails if e['is_unread']),
            'email_ids': [e['id'] for e in large_emails],
        }]

    def _find_newsletters(self):
        newsletters = [e for e in self._emails if e['has_unsubscribe']]
        if len(newsletters) < config.MIN_PATTERN_COUNT:
            return []
        unread_newsletters = [e for e in newsletters if e['is_unread']]
        result = [{
            'type': 'newsletter',
            'key': 'all_newsletters',
            'display': 'Newsletters & Mailing Lists',
            'count': len(newsletters),
            'size_bytes': sum(e['size_bytes'] for e in newsletters),
            'unread': len(unread_newsletters),
            'email_ids': [e['id'] for e in newsletters],
        }]
        if len(unread_newsletters) >= config.MIN_PATTERN_COUNT:
            result.append({
                'type': 'newsletter',
                'key': 'unread_newsletters',
                'display': 'Unread Newsletters',
                'count': len(unread_newsletters),
                'size_bytes': sum(e['size_bytes'] for e in unread_newsletters),
                'unread': len(unread_newsletters),
                'email_ids': [e['id'] for e in unread_newsletters],
            })
        return result

    def _generate_summary(self):
        unread = sum(1 for e in self._emails if e['is_unread'])
        newsletters = sum(1 for e in self._emails if e['has_unsubscribe'])
        starred = sum(1 for e in self._emails if e['is_starred'])
        recommended = sum(1 for e in self._emails
                          if e.get('scored', {}).get('recommend_delete'))

        sender_counts = defaultdict(int)
        for e in self._emails:
            sender_counts[e['sender_domain']] += 1
        top_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            'total_emails': len(self._emails),
            'total_size_mb': round(sum(e['size_bytes'] for e in self._emails) / (1024 * 1024), 2),
            'unread_count': unread,
            'newsletter_count': newsletters,
            'starred_count': starred,
            'recommended_delete_count': recommended,
            'engaged_sender_count': len(self.engaged_senders),
            'top_senders': [{'domain': d, 'count': c} for d, c in top_senders],
        }

    def _empty_analysis(self):
        return {
            'total_emails': 0,
            'total_size_bytes': 0,
            'preset': self.preset,
            'presets_available': [
                {'key': k, 'label': v['label'], 'description': v['description']}
                for k, v in config.CLEANUP_PRESETS.items()
            ],
            'patterns': {
                'by_recommended_cleanup': [],
                'by_sender_domain': [],
                'by_sender_email': [],
                'by_category': [],
                'by_age': [],
                'by_size': [],
                'newsletters': [],
            },
            'summary': {
                'total_emails': 0,
                'total_size_mb': 0,
                'unread_count': 0,
                'newsletter_count': 0,
                'starred_count': 0,
                'recommended_delete_count': 0,
                'engaged_sender_count': len(self.engaged_senders),
                'top_senders': [],
            },
        }

    def get_email_samples(self, email_ids, limit=10):
        sample_ids = email_ids[:limit]
        samples = []
        messages = self.client.get_messages_batch(
            sample_ids,
            format='metadata',
            metadata_headers=['From', 'Subject', 'Date']
        )
        for msg in messages:
            if 'error' not in msg:
                parsed = self._parse_message(msg)
                samples.append({
                    'id': parsed['id'],
                    'from': parsed['sender_email'],
                    'from_name': parsed['sender_name'],
                    'subject': parsed['subject'],
                    'date': parsed['date'].strftime('%Y-%m-%d %H:%M'),
                    'snippet': msg.get('snippet', '')[:100],
                    'size_kb': round(parsed['size_bytes'] / 1024, 1),
                })
        return samples

    def get_emails_for_pattern(self, pattern_type, pattern_key):
        if not self._analysis_cache:
            return []
        patterns = self._analysis_cache['patterns']
        for category in patterns.values():
            for pattern in category:
                if pattern['type'] == pattern_type and pattern['key'] == pattern_key:
                    return pattern['email_ids']
        return []
